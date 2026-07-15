"""GL Phase 1 (S2) — chart-of-accounts seed + gl_settings config store.

Plan gates (docs/design/gl-phase1-implementation-plan.md §S2): seed
idempotency; every role has exactly one active system account; the
SALES_FALLBACK/4000 fallback exists; config-store defaults load; the engine
role-resolution helper returns the mapped account. SQLite (no triggers
needed — nothing here posts).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from gdx_dispatch.modules.ledger.coa import (
    DEFAULT_COA,
    DEFAULT_EXPENSE_CATEGORY_CODES,
    LedgerConfigError,
    resolve_role_account,
    seed_coa,
)
from gdx_dispatch.modules.ledger.models import (
    ALL_ROLES,
    ROLE_AR,
    ROLE_SALES_FALLBACK,
    GlAccount,
    GlSettings,
)
from gdx_dispatch.modules.ledger.service import (
    DEFAULT_CREDIT_REASON_ROLE_MAP,
    DEFAULT_PAYMENT_METHOD_ROLE_MAP,
    ensure_gl_seed,
    ensure_gl_settings,
    get_gl_settings,
    normalize_payment_method,
    resolve_payment_method_role,
    validate_role_map,
)
from gdx_dispatch.routers.expenses import _EXPENSE_CATEGORIES

COMPANY = "11111111-1111-1111-1111-111111111111"
OTHER_COMPANY = "22222222-2222-2222-2222-222222222222"


def _accounts(db, company_id=COMPANY):
    return db.scalars(
        select(GlAccount).where(GlAccount.company_id == company_id)
    ).all()


# ---------------------------------------------------------------------------
# DEFAULT_COA static sanity — the table and the role list must never drift.
# ---------------------------------------------------------------------------

def test_default_coa_roles_exactly_cover_all_roles():
    table_roles = [role for *_x, role, _sys in DEFAULT_COA if role is not None]
    assert sorted(table_roles) == sorted(ALL_ROLES)
    assert len(table_roles) == len(set(table_roles)), "duplicate role in DEFAULT_COA"


def test_default_coa_every_role_row_is_system_and_vice_versa():
    for code, _name, _type, role, is_system in DEFAULT_COA:
        assert (role is not None) == is_system, f"account {code}: role/is_system mismatch"


def test_default_coa_codes_unique():
    codes = [row[0] for row in DEFAULT_COA]
    assert len(codes) == len(set(codes))


def test_expense_category_codes_cover_operational_categories():
    """The seed map must track routers/expenses.py's category list exactly —
    a category added there without a CoA home would silently fall back."""
    assert set(DEFAULT_EXPENSE_CATEGORY_CODES) == set(_EXPENSE_CATEGORIES)
    seeded_codes = {row[0] for row in DEFAULT_COA}
    assert set(DEFAULT_EXPENSE_CATEGORY_CODES.values()) <= seeded_codes


# ---------------------------------------------------------------------------
# Seed behavior
# ---------------------------------------------------------------------------

def test_seed_idempotent(tenant_db):
    first = seed_coa(tenant_db, COMPANY)
    tenant_db.commit()
    assert first == len(DEFAULT_COA)
    again = seed_coa(tenant_db, COMPANY)
    assert again == 0
    assert len(_accounts(tenant_db)) == len(DEFAULT_COA)


def test_every_role_has_exactly_one_active_system_account(tenant_db):
    seed_coa(tenant_db, COMPANY)
    for role in ALL_ROLES:
        owners = [
            a
            for a in _accounts(tenant_db)
            if a.role == role and a.is_system and a.active
        ]
        assert len(owners) == 1, f"role {role} owned by {len(owners)} accounts"


def test_sales_fallback_seeded_as_4000(tenant_db):
    seed_coa(tenant_db, COMPANY)
    acct = resolve_role_account(tenant_db, COMPANY, ROLE_SALES_FALLBACK)
    assert acct.code == "4000"
    assert acct.type == "revenue"


def test_reseed_after_renumber_does_not_duplicate_system_row(tenant_db):
    """System rows are keyed by role: renaming/renumbering 1200 AR must not
    resurrect a second AR on re-seed."""
    seed_coa(tenant_db, COMPANY)
    ar = resolve_role_account(tenant_db, COMPANY, ROLE_AR)
    ar.code, ar.name = "1250", "Trade Receivables"
    tenant_db.flush()

    assert seed_coa(tenant_db, COMPANY) == 0
    owners = [a for a in _accounts(tenant_db) if a.role == ROLE_AR]
    assert len(owners) == 1
    assert owners[0].code == "1250"


def test_reseed_does_not_resurrect_deactivated_nonsystem_row(tenant_db):
    """Non-system rows seed only into an empty CoA; deactivation is not
    deletion, so the seed must not re-add (or duplicate) a deactivated
    account."""
    seed_coa(tenant_db, COMPANY)
    fuel = next(a for a in _accounts(tenant_db) if a.code == "6100")
    fuel.active = False
    tenant_db.flush()

    assert seed_coa(tenant_db, COMPANY) == 0
    fuels = [a for a in _accounts(tenant_db) if a.code == "6100"]
    assert len(fuels) == 1
    assert fuels[0].active is False


def test_reseed_after_nonsystem_renumber_does_not_resurrect(tenant_db):
    """Audit round 1: the operator renumbers "6100 Fuel" → 6105; a re-seed
    must NOT resurrect a fresh 6100 (non-system rows get exactly one shot,
    at first boot)."""
    seed_coa(tenant_db, COMPANY)
    fuel = next(a for a in _accounts(tenant_db) if a.code == "6100")
    fuel.code = "6105"
    tenant_db.flush()

    assert seed_coa(tenant_db, COMPANY) == 0
    codes = [a.code for a in _accounts(tenant_db)]
    assert "6100" not in codes
    assert codes.count("6105") == 1


def test_reseed_tops_up_missing_system_role_only(tenant_db):
    """A later release adding a new role must reach existing installs: system
    rows top up by role, without re-touching non-system rows. Simulated by
    hard-deleting a system row (SQLite — no immutability triggers here)."""
    seed_coa(tenant_db, COMPANY)
    before = len(_accounts(tenant_db))
    wages = next(a for a in _accounts(tenant_db) if a.role == "WAGES")
    tenant_db.delete(wages)
    tenant_db.flush()

    assert seed_coa(tenant_db, COMPANY) == 1
    accounts = _accounts(tenant_db)
    assert len(accounts) == before
    restored = [a for a in accounts if a.role == "WAGES"]
    assert len(restored) == 1 and restored[0].is_system


def test_seed_is_company_scoped(tenant_db):
    seed_coa(tenant_db, COMPANY)
    assert _accounts(tenant_db, OTHER_COMPANY) == []
    with pytest.raises(LedgerConfigError):
        resolve_role_account(tenant_db, OTHER_COMPANY, ROLE_AR)


# ---------------------------------------------------------------------------
# Role resolution — loud on every failure mode
# ---------------------------------------------------------------------------

def test_resolve_returns_mapped_account(tenant_db):
    seed_coa(tenant_db, COMPANY)
    acct = resolve_role_account(tenant_db, COMPANY, ROLE_AR)
    assert acct.code == "1200"
    assert acct.is_system is True


def test_resolve_unknown_role_raises(tenant_db):
    with pytest.raises(LedgerConfigError, match="unknown"):
        resolve_role_account(tenant_db, COMPANY, "NOT_A_ROLE")


def test_resolve_unseeded_raises(tenant_db):
    with pytest.raises(LedgerConfigError, match="no active system account"):
        resolve_role_account(tenant_db, COMPANY, ROLE_AR)


def test_resolve_deactivated_owner_raises(tenant_db):
    seed_coa(tenant_db, COMPANY)
    resolve_role_account(tenant_db, COMPANY, ROLE_AR).active = False
    tenant_db.flush()
    with pytest.raises(LedgerConfigError, match="no active system account"):
        resolve_role_account(tenant_db, COMPANY, ROLE_AR)


def test_duplicate_active_system_role_rejected_by_db(tenant_db):
    """uq_gl_accounts_active_system_role (partial unique index, created by
    create_all here / migration 020 on existing DBs) makes ambiguous role
    ownership unrepresentable — SQLite enforces partial indexes too."""
    seed_coa(tenant_db, COMPANY)
    tenant_db.add(
        GlAccount(
            code="1201",
            name="Second AR",
            type="asset",
            role=ROLE_AR,
            is_system=True,
            company_id=COMPANY,
        )
    )
    with pytest.raises(IntegrityError):
        tenant_db.flush()
    tenant_db.rollback()


def test_inactive_duplicate_role_is_allowed_and_resolution_still_works(tenant_db):
    """The unique index is partial on (is_system AND active): a deactivated
    old owner may share the role; resolution returns the active one."""
    seed_coa(tenant_db, COMPANY)
    tenant_db.add(
        GlAccount(
            code="1199",
            name="Old AR",
            type="asset",
            role=ROLE_AR,
            is_system=True,
            active=False,
            company_id=COMPANY,
        )
    )
    tenant_db.flush()
    assert resolve_role_account(tenant_db, COMPANY, ROLE_AR).code == "1200"


# ---------------------------------------------------------------------------
# gl_settings config store
# ---------------------------------------------------------------------------

def test_ensure_gl_seed_materializes_defaults(tenant_db):
    settings = ensure_gl_seed(tenant_db, COMPANY)
    tenant_db.commit()

    assert settings.ledger_posting_enabled is False
    assert settings.reporting_basis == "accrual"
    assert settings.tax_basis == "cash"
    assert settings.inventory_treatment == "expense"
    assert settings.cutover_month is None
    assert settings.payment_method_role_map == DEFAULT_PAYMENT_METHOD_ROLE_MAP
    assert settings.credit_reason_role_map == DEFAULT_CREDIT_REASON_ROLE_MAP
    assert settings.cpa_review == {}


def test_ensure_gl_settings_singleton_and_preserves_edits(tenant_db):
    """Audit round 1 hardening: commit + expire_all so the re-ensure reads
    persisted state, not the session identity map — this proves the JSON
    reassignment actually round-trips through the DB."""
    row = ensure_gl_seed(tenant_db, COMPANY)
    row_id = row.id
    edited = dict(row.payment_method_role_map)
    edited["cash"] = "OPERATING_BANK"
    row.payment_method_role_map = edited
    tenant_db.commit()
    tenant_db.expire_all()

    again = ensure_gl_settings(tenant_db, COMPANY)
    tenant_db.commit()
    tenant_db.expire_all()

    fresh = get_gl_settings(tenant_db, COMPANY)
    assert fresh.id == row_id
    assert fresh.payment_method_role_map["cash"] == "OPERATING_BANK"
    assert (
        len(tenant_db.scalars(select(GlSettings)).all()) == 1
    ), "singleton violated"


def test_ensure_tops_up_new_default_keys_without_touching_edits(tenant_db):
    """A default key added by a later release must reach existing installs;
    operator-edited values must survive. (Keys are topped up — disabling a
    method means remapping it, not deleting the key.)"""
    row = ensure_gl_seed(tenant_db, COMPANY)
    pruned = {
        k: v for k, v in row.payment_method_role_map.items() if k != "zelle"
    }
    pruned["cash"] = "OPERATING_BANK"  # operator edit
    row.payment_method_role_map = pruned
    tenant_db.commit()
    tenant_db.expire_all()

    ensure_gl_settings(tenant_db, COMPANY)
    tenant_db.commit()
    tenant_db.expire_all()

    fresh = get_gl_settings(tenant_db, COMPANY)
    assert fresh.payment_method_role_map["zelle"] == DEFAULT_PAYMENT_METHOD_ROLE_MAP["zelle"]
    assert fresh.payment_method_role_map["cash"] == "OPERATING_BANK"


def test_expense_map_topup_adds_only_missing_categories(tenant_db):
    settings = ensure_gl_seed(tenant_db, COMPANY)
    kept = {
        k: v for k, v in settings.expense_category_account_map.items() if k != "Fuel"
    }
    sentinel = kept["Advertising"] = "operator-chosen-id"
    settings.expense_category_account_map = kept
    tenant_db.commit()
    tenant_db.expire_all()

    ensure_gl_settings(tenant_db, COMPANY)
    tenant_db.commit()
    tenant_db.expire_all()

    fresh = get_gl_settings(tenant_db, COMPANY)
    fuel_acct_id = fresh.expense_category_account_map["Fuel"]
    fuel = next(a for a in _accounts(tenant_db) if str(a.id) == fuel_acct_id)
    assert fuel.code == "6100"
    assert fresh.expense_category_account_map["Advertising"] == sentinel


def test_expense_category_map_resolves_to_seeded_accounts(tenant_db):
    settings = ensure_gl_seed(tenant_db, COMPANY)
    assert set(settings.expense_category_account_map) == set(_EXPENSE_CATEGORIES)
    accounts_by_id = {str(a.id): a for a in _accounts(tenant_db)}
    for category, account_id in settings.expense_category_account_map.items():
        acct = accounts_by_id.get(account_id)
        assert acct is not None, f"{category} maps to a nonexistent account"
        assert acct.code == DEFAULT_EXPENSE_CATEGORY_CODES[category]


def test_ensure_settings_without_seed_raises(tenant_db):
    """The expense map needs account ids — calling ensure_gl_settings on an
    unseeded CoA must fail loudly, not write a half-empty map."""
    with pytest.raises(LedgerConfigError, match="seeded accounts missing"):
        ensure_gl_settings(tenant_db, COMPANY)


def test_default_maps_use_valid_roles():
    validate_role_map("payment_method_role_map", DEFAULT_PAYMENT_METHOD_ROLE_MAP)
    validate_role_map("credit_reason_role_map", DEFAULT_CREDIT_REASON_ROLE_MAP)


def test_validate_role_map_rejects_unknown_role():
    with pytest.raises(LedgerConfigError, match="unknown roles"):
        validate_role_map("m", {"cash": "PETTY_CASH_TYPO"})


def test_get_gl_settings_returns_none_before_ensure(tenant_db):
    assert get_gl_settings(tenant_db, COMPANY) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Cash", "cash"),
        (" ACH ", "ach"),
        ("check", "check"),
        (None, "other"),
        ("", "other"),
    ],
)
def test_normalize_payment_method(raw, expected):
    assert normalize_payment_method(raw) == expected


def test_resolve_payment_method_role(tenant_db):
    """Every method the app actually mints resolves — including "quickbooks"
    (QB sync, core/quickbooks.py) — and unknown/legacy free-text falls back
    to the "other" entry, never a KeyError."""
    settings = ensure_gl_seed(tenant_db, COMPANY)
    assert resolve_payment_method_role(settings, "Check") == "UNDEPOSITED"
    assert resolve_payment_method_role(settings, "quickbooks") == "OPERATING_BANK"
    assert (
        resolve_payment_method_role(settings, "crypto-legacy-freetext")
        == DEFAULT_PAYMENT_METHOD_ROLE_MAP["other"]
    )

    settings.payment_method_role_map = {"cash": "UNDEPOSITED"}  # no "other"
    with pytest.raises(LedgerConfigError, match='"other" fallback'):
        resolve_payment_method_role(settings, "crypto")


def test_gl_models_registered_on_models_package():
    """models/__init__ registers ledger models inside a try/except
    ImportError — a real import error there would silently un-register every
    gl_* table from create_orm_tables. This test makes that failure loud."""
    import gdx_dispatch.models as m

    for name in ("GlAccount", "GlJournalEntry", "GlJournalLine", "GlPeriodLock", "GlSettings"):
        assert hasattr(m, name), f"{name} missing from gdx_dispatch.models — ledger import silently failed"
