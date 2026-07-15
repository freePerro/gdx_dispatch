"""GL Phase 1 (S4.5) — Accounting Settings API.

Plan gates: config round-trips to the S2 store; guardrail lock after the
flag flips or the first entry posts; role-protected accounts reject
delete/deactivate; the enable switch demands the exact confirm phrase.
Endpoint functions are called directly with a session + user dict (house
pattern, same as test_invoices.py).
"""
from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.modules.ledger.coa import DEFAULT_COA
from gdx_dispatch.modules.ledger.engine import PostingEvent, PostingLine, post_for_event
from gdx_dispatch.modules.ledger.router import (
    ENABLE_CONFIRM_PHRASE,
    AccountCreateIn,
    AccountPatchIn,
    CpaReviewIn,
    EnablePostingIn,
    SettingsPatchIn,
    create_account,
    disable_posting,
    enable_posting,
    get_accounting_settings,
    initialize_accounting,
    patch_account,
    patch_accounting_settings,
    stamp_cpa_review,
)
from gdx_dispatch.modules.ledger.models import GlAccount
from gdx_dispatch.modules.ledger.service import ensure_gl_seed

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"tenant_id": COMPANY, "sub": "tester"}


def _get(db):
    return get_accounting_settings(db=db, user=USER, _perm=None)


def _init(db):
    return initialize_accounting(db=db, user=USER, _perm=None)


def _patch(db, **fields):
    return patch_accounting_settings(SettingsPatchIn(**fields), db=db, user=USER, _perm=None)


def _enable(db, confirm=ENABLE_CONFIRM_PHRASE):
    return enable_posting(EnablePostingIn(confirm=confirm), db=db, user=USER, _perm=None)


def _post_entry(db):
    entry = post_for_event(
        db,
        PostingEvent(
            company_id=COMPANY, source_type="invoice", source_id=str(uuid4()),
            event="issued", effective_at=dt.date(2026, 7, 1),
            lines=(
                PostingLine(amount_cents=100, role="AR"),
                PostingLine(amount_cents=-100, role="SALES_FALLBACK"),
            ),
        ),
    )
    db.commit()
    return entry


# ---------------------------------------------------------------------------
# GET — first touch seeds
# ---------------------------------------------------------------------------

def test_get_is_a_pure_read_before_initialize(tenant_db):
    """Audit round 1: a read-only viewer's GET must not seed the CoA."""
    payload = _get(tenant_db)
    assert payload["seeded"] is False
    assert payload["accounts"] == [] and payload["settings"] is None
    assert tenant_db.scalars(select(GlAccount)).all() == []


def test_initialize_seeds_full_payload_and_audits(tenant_db):
    payload = _init(tenant_db)
    assert payload["seeded"] is True
    assert len(payload["accounts"]) == len(DEFAULT_COA)
    assert payload["settings"]["ledger_posting_enabled"] is False
    assert payload["locked_fields"] == {}
    assert payload["entries_exist"] is False
    assert "AR" in payload["roles"]
    assert payload["enable_confirm_phrase"] == ENABLE_CONFIRM_PHRASE
    actions = {a.action for a in tenant_db.scalars(select(AuditLog))}
    assert "gl_settings_initialized" in actions
    # idempotent — second call neither duplicates nor re-audits
    again = _init(tenant_db)
    assert len(again["accounts"]) == len(DEFAULT_COA)


# ---------------------------------------------------------------------------
# PATCH — round-trips + validation
# ---------------------------------------------------------------------------

def test_patch_policy_round_trips(tenant_db):
    _init(tenant_db)
    payload = _patch(
        tenant_db,
        tax_basis="accrual",
        inventory_treatment="capitalize",
        cutover_month=dt.date(2026, 9, 17),
        entity_type="S-Corp",
    )
    s = payload["settings"]
    assert s["tax_basis"] == "accrual"
    assert s["inventory_treatment"] == "capitalize"
    assert s["cutover_month"] == "2026-09-01"  # snapped to first of month
    assert s["entity_type"] == "S-Corp"
    # persisted, not just echoed
    fresh = _get(tenant_db)
    assert fresh["settings"]["cutover_month"] == "2026-09-01"


def test_patch_rejects_bad_enums(tenant_db):
    _init(tenant_db)
    with pytest.raises(HTTPException) as exc:
        _patch(tenant_db, reporting_basis="vibes")
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        _patch(tenant_db, inventory_treatment="maybe")
    assert exc.value.status_code == 422


def test_patch_payment_map_validates_roles_and_fallback(tenant_db):
    _init(tenant_db)
    with pytest.raises(HTTPException) as exc:
        _patch(tenant_db, payment_method_role_map={"cash": "NOT_A_ROLE", "other": "UNDEPOSITED"})
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        _patch(tenant_db, payment_method_role_map={"cash": "UNDEPOSITED"})  # no "other"
    assert exc.value.status_code == 422 and "other" in exc.value.detail

    ok = _patch(tenant_db, payment_method_role_map={"cash": "OPERATING_BANK", "other": "UNDEPOSITED"})
    assert ok["settings"]["payment_method_role_map"]["cash"] == "OPERATING_BANK"


def test_patch_expense_map_rejects_dangling_account(tenant_db):
    payload = _init(tenant_db)
    good_map = dict(payload["settings"]["expense_category_account_map"])
    good_map["Fuel"] = str(uuid4())  # not a real account
    with pytest.raises(HTTPException) as exc:
        _patch(tenant_db, expense_category_account_map=good_map)
    assert exc.value.status_code == 422


def test_attest_opening_bank_stamps_actor(tenant_db):
    _init(tenant_db)
    payload = _patch(tenant_db, attest_opening_bank=True)
    assert payload["settings"]["opening_bank_attested_at"] is not None
    assert payload["settings"]["opening_bank_attested_by"] == "tester"


# ---------------------------------------------------------------------------
# Guardrails — QBO parity locks
# ---------------------------------------------------------------------------

def test_locked_fields_after_enable(tenant_db):
    _init(tenant_db)
    payload = _enable(tenant_db)
    assert set(payload["locked_fields"]) == {
        "inventory_treatment", "cutover_month", "payment_method_role_map",
    }
    for kwargs in (
        {"inventory_treatment": "capitalize"},
        {"cutover_month": dt.date(2026, 8, 1)},
        {"payment_method_role_map": {"other": "UNDEPOSITED"}},
    ):
        with pytest.raises(HTTPException) as exc:
            _patch(tenant_db, **kwargs)
        assert exc.value.status_code == 409
    # non-guarded fields stay editable
    assert _patch(tenant_db, entity_type="LLC")["settings"]["entity_type"] == "LLC"


def test_locked_fields_after_first_posted_entry_even_flag_off(tenant_db):
    _init(tenant_db)
    _post_entry(tenant_db)
    payload = _get(tenant_db)
    assert payload["entries_exist"] is True
    assert "inventory_treatment" in payload["locked_fields"]
    with pytest.raises(HTTPException) as exc:
        _patch(tenant_db, inventory_treatment="capitalize")
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# Master switch — one-way door
# ---------------------------------------------------------------------------

def test_enable_requires_exact_phrase(tenant_db):
    _init(tenant_db)
    with pytest.raises(HTTPException) as exc:
        _enable(tenant_db, confirm="enable ledger posting")
    assert exc.value.status_code == 422
    assert _enable(tenant_db)["settings"]["ledger_posting_enabled"] is True


def test_disable_only_while_ledger_empty(tenant_db):
    _init(tenant_db)
    _enable(tenant_db)
    payload = disable_posting(db=tenant_db, user=USER, _perm=None)
    assert payload["settings"]["ledger_posting_enabled"] is False

    _enable(tenant_db)
    _post_entry(tenant_db)
    with pytest.raises(HTTPException) as exc:
        disable_posting(db=tenant_db, user=USER, _perm=None)
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# Accounts — protected system rows, deactivate-not-delete
# ---------------------------------------------------------------------------

def _account_by_code(db, payload, code):
    return next(a for a in payload["accounts"] if a["code"] == code)


def test_system_account_rename_ok_deactivate_forbidden(tenant_db):
    payload = _init(tenant_db)
    ar = _account_by_code(tenant_db, payload, "1200")
    assert ar["is_system"] and ar["role"] == "AR"

    renamed = patch_account(
        UUID(ar["id"]), AccountPatchIn(code="1250", name="Trade Receivables"),
        db=tenant_db, user=USER, _perm=None,
    )
    assert renamed["code"] == "1250"

    with pytest.raises(HTTPException) as exc:
        patch_account(UUID(ar["id"]), AccountPatchIn(active=False), db=tenant_db, user=USER, _perm=None)
    assert exc.value.status_code == 409
    assert "system account" in exc.value.detail


def test_nonsystem_account_deactivates_and_role_is_never_settable(tenant_db):
    payload = _init(tenant_db)
    # 4200 Parts Revenue: non-system AND not referenced by the expense map
    parts_rev = _account_by_code(tenant_db, payload, "4200")
    updated = patch_account(UUID(parts_rev["id"]), AccountPatchIn(active=False), db=tenant_db, user=USER, _perm=None)
    assert updated["active"] is False
    # AccountPatchIn simply has no role field — reassignment is unrepresentable
    assert "role" not in AccountPatchIn.model_fields
    assert "role" not in AccountCreateIn.model_fields


def test_create_account_and_code_clash(tenant_db):
    _init(tenant_db)
    created = create_account(
        AccountCreateIn(code="6600", name="Shop Supplies", type="expense"),
        db=tenant_db, user=USER, _perm=None,
    )
    assert created["role"] is None and created["is_system"] is False

    with pytest.raises(HTTPException) as exc:
        create_account(
            AccountCreateIn(code="6600", name="Duplicate", type="expense"),
            db=tenant_db, user=USER, _perm=None,
        )
    assert exc.value.status_code == 409

    with pytest.raises(HTTPException) as exc:
        create_account(
            AccountCreateIn(code="6601", name="Bad type", type="wishes"),
            db=tenant_db, user=USER, _perm=None,
        )
    assert exc.value.status_code == 422


def test_account_cross_company_is_404(tenant_db):
    other_user = {"tenant_id": "22222222-2222-2222-2222-222222222222", "sub": "intruder"}
    payload = _init(tenant_db)
    ar = _account_by_code(tenant_db, payload, "1200")
    with pytest.raises(HTTPException) as exc:
        patch_account(UUID(ar["id"]), AccountPatchIn(name="Hijack"), db=tenant_db, user=other_user, _perm=None)
    assert exc.value.status_code == 404


def test_renumber_to_existing_active_code_conflicts(tenant_db):
    payload = _init(tenant_db)
    fuel = _account_by_code(tenant_db, payload, "6100")
    with pytest.raises(HTTPException) as exc:
        patch_account(UUID(fuel["id"]), AccountPatchIn(code="6200"), db=tenant_db, user=USER, _perm=None)
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# CPA review stamps + audit trail
# ---------------------------------------------------------------------------

def test_cpa_review_stamps(tenant_db):
    _init(tenant_db)
    payload = stamp_cpa_review(
        CpaReviewIn(keys=["inventory_treatment", "payment_method_role_map"]),
        db=tenant_db, user=USER, _perm=None,
    )
    stamps = payload["settings"]["cpa_review"]
    assert stamps["inventory_treatment"]["by"] == "tester"
    assert stamps["payment_method_role_map"]["reviewed_at"]


def test_changes_are_audit_logged(tenant_db):
    _init(tenant_db)
    _patch(tenant_db, tax_basis="accrual")
    _enable(tenant_db)
    actions = {
        a.action
        for a in tenant_db.scalars(select(AuditLog).where(AuditLog.tenant_id == COMPANY))
    }
    assert {"gl_settings_updated", "gl_posting_enabled"} <= actions


# ---------------------------------------------------------------------------
# Audit round 1 — guardrail holes closed
# ---------------------------------------------------------------------------

def test_entity_type_can_be_cleared(tenant_db):
    _init(tenant_db)
    assert _patch(tenant_db, entity_type="S-Corp")["settings"]["entity_type"] == "S-Corp"
    payload = _patch(tenant_db, clear_entity_type=True)
    assert payload["settings"]["entity_type"] is None


def test_opening_bank_attestation_can_be_cleared(tenant_db):
    _init(tenant_db)
    _patch(tenant_db, attest_opening_bank=True)
    payload = _patch(tenant_db, clear_opening_bank_attestation=True)
    assert payload["settings"]["opening_bank_attested_at"] is None
    assert payload["settings"]["opening_bank_attested_by"] is None


def test_expense_map_rejects_wrong_type_and_inactive_accounts(tenant_db):
    payload = _init(tenant_db)
    ar = _account_by_code(tenant_db, payload, "1200")  # asset, not expense
    bad_map = dict(payload["settings"]["expense_category_account_map"])
    bad_map["Fuel"] = ar["id"]
    with pytest.raises(HTTPException) as exc:
        _patch(tenant_db, expense_category_account_map=bad_map)
    assert exc.value.status_code == 422 and "not expense" in str(exc.value.detail)

    # an inactive EXPENSE account: create one (every seeded expense account
    # is either mapped or system), deactivate it, then try mapping to it
    scrap = create_account(
        AccountCreateIn(code="6800", name="Scrap", type="expense"),
        db=tenant_db, user=USER, _perm=None,
    )
    patch_account(UUID(scrap["id"]), AccountPatchIn(active=False), db=tenant_db, user=USER, _perm=None)
    bad_map = dict(payload["settings"]["expense_category_account_map"])
    bad_map["Fuel"] = scrap["id"]
    with pytest.raises(HTTPException) as exc:
        _patch(tenant_db, expense_category_account_map=bad_map)
    assert exc.value.status_code == 422 and "deactivated" in str(exc.value.detail)


def test_deactivating_a_mapped_expense_account_conflicts(tenant_db):
    payload = _init(tenant_db)
    fuel = _account_by_code(tenant_db, payload, "6100")  # mapped to "Fuel"
    with pytest.raises(HTTPException) as exc:
        patch_account(UUID(fuel["id"]), AccountPatchIn(active=False), db=tenant_db, user=USER, _perm=None)
    assert exc.value.status_code == 409 and "Fuel" in exc.value.detail


def test_reactivation_respects_code_clash(tenant_db):
    payload = _init(tenant_db)
    parts_rev = _account_by_code(tenant_db, payload, "4200")  # unmapped — deactivatable
    patch_account(UUID(parts_rev["id"]), AccountPatchIn(active=False), db=tenant_db, user=USER, _perm=None)
    create_account(
        AccountCreateIn(code="4200", name="New Parts Revenue", type="revenue"),
        db=tenant_db, user=USER, _perm=None,
    )
    with pytest.raises(HTTPException) as exc:
        patch_account(UUID(parts_rev["id"]), AccountPatchIn(active=True), db=tenant_db, user=USER, _perm=None)
    assert exc.value.status_code == 409


def test_whitespace_code_rejected(tenant_db):
    _init(tenant_db)
    with pytest.raises(ValueError):
        AccountCreateIn(code="   ", name="Blank", type="expense")
    created = create_account(
        AccountCreateIn(code=" 6700 ", name="  Trimmed  ", type="expense"),
        db=tenant_db, user=USER, _perm=None,
    )
    assert created["code"] == "6700" and created["name"] == "Trimmed"


def test_patch_before_initialize_conflicts(tenant_db):
    with pytest.raises(HTTPException) as exc:
        _patch(tenant_db, tax_basis="accrual")
    assert exc.value.status_code == 409


def test_permission_keys_exist_in_catalog():
    """Direct-call tests bypass the auth deps — at minimum the keys the
    router asks for must exist in the RBAC catalog, or every non-owner
    would 403."""
    from gdx_dispatch.core.permissions import PERMISSIONS

    keys = {k for k, _label, _cat in PERMISSIONS}
    assert {"accounting.read", "accounting.write"} <= keys


def test_routes_are_mounted():
    """app.py guards the router import with try/except — a silent
    ImportError would ship a booting app whose nav item 404s."""
    from gdx_dispatch.app import create_app

    schema = create_app().openapi()
    for path in (
        "/api/accounting/settings",
        "/api/accounting/settings/initialize",
        "/api/accounting/settings/enable-posting",
        "/api/accounting/accounts",
    ):
        assert path in schema["paths"], f"{path} not mounted"
