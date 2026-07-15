"""Config-store access for the ledger (S2, plan guiding rule 3).

``ensure_gl_seed()`` is the one entry point callers use (S4.5 settings router,
tests): it seeds the starter CoA and materializes the ``gl_settings``
singleton with defaults. Defaults live HERE, not in column defaults, so there
is a single source of truth the settings page and the engine share.

Nothing in this module commits — callers own the transaction.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.ledger.coa import (
    DEFAULT_EXPENSE_CATEGORY_CODES,
    LedgerConfigError,
    seed_coa,
)
from gdx_dispatch.modules.ledger.models import (
    ALL_ROLES,
    ROLE_DISCOUNTS,
    ROLE_OPERATING_BANK,
    ROLE_REFUNDS,
    ROLE_UNDEPOSITED,
    GlAccount,
    GlSettings,
)

# payment method (normalized) → role. Cash/check/card sit in Undeposited
# Funds until Phase 2 statement matching clears them into the bank; Zelle/
# Venmo/ACH land directly in the operating account. [CPA] reviewable, edited
# on the Accounting Settings page (S4.5) — data, not code.
#
# Key domain = the 7 UI options (InvoiceDetailView paymentMethods, lowercased
# at write time, invoices.py) PLUS "quickbooks", minted by the QB sync
# (core/quickbooks.py, modules/quickbooks/sync.py) — historical QB-pulled
# payments carry it, so P8 backfill and S6 posting will meet it. QB-synced
# money already landed in the bank by the time we learn of it. [CPA]
# Methods not in the map resolve via the "other" key (see
# resolve_payment_method_role) — never a silent KeyError at posting time.
DEFAULT_PAYMENT_METHOD_ROLE_MAP: dict[str, str] = {
    "cash": ROLE_UNDEPOSITED,
    "check": ROLE_UNDEPOSITED,
    "card": ROLE_UNDEPOSITED,
    "zelle": ROLE_OPERATING_BANK,
    "venmo": ROLE_OPERATING_BANK,
    "ach": ROLE_OPERATING_BANK,
    "quickbooks": ROLE_OPERATING_BANK,
    "other": ROLE_UNDEPOSITED,
}

# credit/refund reason → contra-revenue role: 4900 Discounts Given vs 4910
# Refunds & Allowances (spec §4 — the split exists so discounts don't absorb
# warranty/error credits). S7 validates submitted reasons against these keys.
DEFAULT_CREDIT_REASON_ROLE_MAP: dict[str, str] = {
    "discount": ROLE_DISCOUNTS,
    "promotion": ROLE_DISCOUNTS,
    "goodwill": ROLE_DISCOUNTS,
    "warranty": ROLE_REFUNDS,
    "workmanship": ROLE_REFUNDS,
    "billing_error": ROLE_REFUNDS,
    "other": ROLE_REFUNDS,
}


def normalize_payment_method(method: str | None) -> str:
    """Canonical map key for a Payment.method value. The UI sends "Cash"/
    "Check"/…, the column default is lowercase "cash" — normalize so the map
    has one key per method."""
    return (method or "").strip().lower() or "other"


def resolve_payment_method_role(settings: GlSettings, method: str | None) -> str:
    """The role a payment method posts to. Unknown methods fall back to the
    map's "other" entry — explicitly, so a free-text/legacy method value can
    never KeyError mid-posting. A map with no "other" key is a config error.
    """
    mapping = settings.payment_method_role_map or {}
    role = mapping.get(normalize_payment_method(method)) or mapping.get("other")
    if role is None:
        raise LedgerConfigError(
            'payment_method_role_map has no "other" fallback entry — '
            "fix on the Accounting Settings page"
        )
    return role


def _expense_category_map_with_topup(
    session: Session, company_id: str, current: dict | None
) -> dict[str, str]:
    """category → gl_accounts.id (str). Stored as ids because codes are
    operator-renumberable; a dangling id later falls back to EXPENSE_FALLBACK
    at posting time (S8). Existing entries are never touched — only
    categories missing from ``current`` are resolved from the seeded default
    codes (so a category added in a later release reaches existing installs).
    """
    current = dict(current or {})
    wanted = {
        category: code
        for category, code in DEFAULT_EXPENSE_CATEGORY_CODES.items()
        if category not in current
    }
    if not wanted:
        return current

    rows = session.scalars(
        select(GlAccount).where(
            GlAccount.company_id == company_id,
            GlAccount.code.in_(set(wanted.values())),
        )
    ).all()
    by_code: dict[str, GlAccount] = {}
    for a in rows:
        # Codes are not unique (operator may collide one); prefer the seeded
        # flavor deterministically: active first, then oldest.
        cur = by_code.get(a.code)
        if cur is None or (a.active, -a.created_at.timestamp()) > (cur.active, -cur.created_at.timestamp()):
            by_code[a.code] = a
    missing = sorted({code for code in wanted.values() if code not in by_code})
    if missing:
        raise LedgerConfigError(
            f"cannot build expense-category map — seeded accounts missing: {missing}"
        )
    for category, code in wanted.items():
        current[category] = str(by_code[code].id)
    return current


def _with_topup(current: dict | None, defaults: dict) -> dict:
    """Merge default keys a release added later into an existing map without
    touching operator-edited values. Returns a NEW dict — JSON columns only
    persist on reassignment (no MutableDict wrapper); in-place mutation is
    silently lost."""
    merged = dict(defaults)
    merged.update(current or {})
    return merged


def validate_role_map(map_name: str, mapping: dict) -> None:
    """Every value must be a known role — a typo'd role must fail at save
    time (S4.5 PATCH) or seed time, never at posting time."""
    bad = {k: v for k, v in mapping.items() if v not in ALL_ROLES}
    if bad:
        raise LedgerConfigError(f"{map_name} maps to unknown roles: {bad}")


def validate_payment_method_map(mapping: dict) -> None:
    """Role validity PLUS the "other" fallback — resolve_payment_method_role
    depends on it. Lives here (not just the router) so every writer of the
    map, present or future, hits the same wall."""
    validate_role_map("payment_method_role_map", mapping)
    if "other" not in mapping:
        raise LedgerConfigError(
            'payment_method_role_map needs an "other" fallback entry'
        )


def get_gl_settings(session: Session, company_id: str) -> GlSettings | None:
    return session.scalars(
        select(GlSettings).where(GlSettings.company_id == company_id)
    ).first()


def ensure_gl_settings(session: Session, company_id: str) -> GlSettings:
    """Get-or-create the per-company singleton and top the maps up with any
    default keys this release added. Idempotent; operator-edited VALUES are
    never overwritten (top-up only adds missing keys — to disable a method or
    reason, remap it, don't delete the key). Requires the CoA to be seeded
    first (the expense map needs account ids) — use ``ensure_gl_seed()``
    unless you know it was.

    JSON persistence note: maps are always REASSIGNED, never mutated in
    place — plain JSON columns (no MutableDict) don't track in-place writes.
    """
    row = get_gl_settings(session, company_id)
    if row is None:
        row = GlSettings(company_id=company_id)
        session.add(row)

    payment_map = _with_topup(row.payment_method_role_map, DEFAULT_PAYMENT_METHOD_ROLE_MAP)
    if payment_map != row.payment_method_role_map:
        row.payment_method_role_map = payment_map
    reason_map = _with_topup(row.credit_reason_role_map, DEFAULT_CREDIT_REASON_ROLE_MAP)
    if reason_map != row.credit_reason_role_map:
        row.credit_reason_role_map = reason_map
    expense_map = _expense_category_map_with_topup(
        session, company_id, row.expense_category_account_map
    )
    if expense_map != row.expense_category_account_map:
        row.expense_category_account_map = expense_map
    if row.cpa_review is None:
        row.cpa_review = {}

    validate_payment_method_map(row.payment_method_role_map)
    validate_role_map("credit_reason_role_map", row.credit_reason_role_map)

    session.flush()
    return row


def ensure_gl_seed(session: Session, company_id: str) -> GlSettings:
    """Seed the starter CoA + materialize the settings singleton. The one
    idempotent bootstrap callers use; flushes, never commits."""
    seed_coa(session, company_id)
    return ensure_gl_settings(session, company_id)


def ledger_posting_enabled(session: Session, company_id: str) -> bool:
    """The master switch (S4+). No settings row (never seeded) = off."""
    row = get_gl_settings(session, company_id)
    return bool(row and row.ledger_posting_enabled)


# ---------------------------------------------------------------------------
# The chokepoint (S4, spec §2) — the ONLY legal writer of Invoice.status.
# ---------------------------------------------------------------------------

# Transient instance attribute the chokepoint stamps and the flush guard
# consumes — how the guard tells a sanctioned transition from a bypass. The
# value is the sanctioned TARGET status (not a boolean): the guard only
# honors it if the flushed status matches, so a stale stamp can't bless a
# different later write.
SANCTION_ATTR = "_gl_transition_sanctioned"
# session.info key listing stamped instances; the guard's after_rollback
# hook clears their stamps (a rolled-back transition must leave no sanction).
SANCTION_REGISTRY_KEY = "gl_sanctioned_invoices"

# (old_status, new_status) → posting callable. EMPTY in S4 — the flag can be
# on with no rules and behavior is still identical. S5+ registers P1/P2…;
# each callable receives (session, invoice, old_status, new_status, actor).
_POSTING_RULES: dict = {}


def transition_invoice_status(session, invoice, new_status: str, *, actor: str | None = None) -> str:
    """Move an invoice to ``new_status`` through the ledger chokepoint.

    S4 semantics: a sanctioned pass-through. With ``ledger_posting_enabled``
    off (the shipped default) the only behavioral delta vs. the old raw
    ``invoice.status = x`` is one SELECT on gl_settings per transition. With
    the flag on, the registered posting rules for (old, new) run inside the
    caller's transaction (none exist until S5) — if a rule raises, the
    caller's rollback unwinds the status change with it.

    Returns the previous status. Idempotent: transitioning to the current
    status is a no-op that still returns it.
    """
    old_status = invoice.status
    if new_status == old_status:
        return old_status

    setattr(invoice, SANCTION_ATTR, new_status)
    session.info.setdefault(SANCTION_REGISTRY_KEY, []).append(invoice)
    invoice.status = new_status

    if ledger_posting_enabled(session, invoice.company_id):
        rule = _POSTING_RULES.get((old_status, new_status))
        if rule is not None:
            rule(session, invoice, old_status, new_status, actor)
    return old_status
