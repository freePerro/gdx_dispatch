"""Starter chart of accounts + role resolution (S2, spec §4).

The CoA ships as a data table and an **idempotent seed**: system rows are
keyed by *role* (renumber-proof — re-seeding after the operator renames
"1200 AR" to "1250 Trade Receivables" must not resurrect a second AR),
non-system rows by *code*. Nothing here posts; the seed only guarantees the
posting engine (S3) can resolve every role in ``ALL_ROLES`` to exactly one
active system account.

CoA composition is convention — Doug + CPA review the seeded values on the
Accounting Settings page (S4.5) before anything posts. [JUDGMENT][CPA]
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.ledger.models import (
    ALL_ROLES,
    ROLE_AR,
    ROLE_CUSTOMER_CREDITS,
    ROLE_DISCOUNTS,
    ROLE_EXPENSE_FALLBACK,
    ROLE_OPENING_EQUITY,
    ROLE_OPERATING_BANK,
    ROLE_PAYROLL_TAX,
    ROLE_REFUNDS,
    ROLE_ROUNDING,
    ROLE_SALES_FALLBACK,
    ROLE_SALES_TAX_PAYABLE,
    ROLE_UNDEPOSITED,
    ROLE_WAGES,
    GlAccount,
)


class LedgerConfigError(RuntimeError):
    """The ledger's configuration cannot support posting (missing/ambiguous
    role, unseeded CoA). Raised loudly — the engine must never guess an
    account."""


# (code, name, type, role, is_system) — spec §4 starter CoA.
# Roles are the engine's stable bindings; is_system rows may be renamed/
# renumbered on the settings page but never deleted or role-reassigned.
DEFAULT_COA: tuple[tuple[str, str, str, str | None, bool], ...] = (
    # -- assets ------------------------------------------------------------
    ("1000", "Operating Bank", "asset", ROLE_OPERATING_BANK, True),
    ("1050", "Undeposited Funds", "asset", ROLE_UNDEPOSITED, True),
    ("1200", "Accounts Receivable", "asset", ROLE_AR, True),
    # -- liabilities --------------------------------------------------------
    ("2100", "Sales Tax Payable", "liability", ROLE_SALES_TAX_PAYABLE, True),
    ("2300", "Customer Credits", "liability", ROLE_CUSTOMER_CREDITS, True),
    # -- equity --------------------------------------------------------------
    ("3000", "Owner Contributions", "equity", None, False),
    ("3100", "Owner Draws", "equity", None, False),
    ("3900", "Retained Earnings", "equity", None, False),
    ("3950", "Opening Balance Equity", "equity", ROLE_OPENING_EQUITY, True),
    # -- revenue -------------------------------------------------------------
    # 4000 is the explicit fallback for NULL/unmapped invoice-line categories
    # (memo-flagged by the engine when used as fallback).
    ("4000", "Service & Repair Revenue", "revenue", ROLE_SALES_FALLBACK, True),
    ("4100", "Installation Revenue", "revenue", None, False),
    ("4200", "Parts Revenue", "revenue", None, False),
    # Contra-revenue, split on purpose: lumping discounts with warranty/error
    # credits misstates the discounts line (spec §4). [CPA] review the split.
    ("4900", "Discounts Given", "revenue", ROLE_DISCOUNTS, True),
    ("4910", "Refunds & Allowances", "revenue", ROLE_REFUNDS, True),
    # -- COGS ------------------------------------------------------------------
    ("5000", "Parts & Materials", "expense", None, False),
    ("5100", "Subcontractors", "expense", None, False),
    # -- opex ---------------------------------------------------------------
    # Payroll runs through an external payroll company; its bank debits land
    # here via Phase 2 R5 statement matching — the business's biggest expense
    # line has a home from day one.
    ("6050", "Wages & Payroll", "expense", ROLE_WAGES, True),
    ("6060", "Payroll Taxes & Fees", "expense", ROLE_PAYROLL_TAX, True),
    ("6100", "Fuel", "expense", None, False),
    ("6200", "Tools & Equipment", "expense", None, False),
    ("6300", "Advertising", "expense", None, False),
    ("6400", "Insurance", "expense", None, False),
    ("6500", "Vehicle Maintenance", "expense", None, False),
    ("6900", "Uncategorized Expense", "expense", ROLE_EXPENSE_FALLBACK, True),
    ("6990", "Rounding Differences", "expense", ROLE_ROUNDING, True),
)

# The eight hardcoded operational expense categories (routers/expenses.py
# _EXPENSE_CATEGORIES) → seeded account code, 1:1 (spec §4). Used only at
# seed time to build gl_settings.expense_category_account_map (which stores
# stable account *ids*, since codes are operator-renumberable).
DEFAULT_EXPENSE_CATEGORY_CODES: dict[str, str] = {
    "Fuel": "6100",
    "Parts/Supplies": "5000",
    "Tools/Equipment": "6200",
    "Advertising": "6300",
    "Insurance": "6400",
    "Vehicle Maintenance": "6500",
    "Subcontractor": "5100",
    "Other": "6900",
}


def seed_coa(session: Session, company_id: str) -> int:
    """Insert missing starter accounts. Idempotent; returns the number of
    accounts inserted. Flushes (so ids are assigned) but never commits — the
    caller owns the transaction.

    Seed identity, deliberately asymmetric (audit round 1):

    - **Non-system rows seed only into an empty CoA.** Their only stable
      field is ``code``, which the operator may renumber — keying re-seeds on
      it would resurrect "6100 Fuel" as a duplicate the moment the operator
      renumbers it to 6105. So they get exactly one shot, at first boot.
    - **System rows top up by role on every run.** Roles are immutable
      identities, so this is safe — and it is how a later release that adds a
      new role to ``ALL_ROLES`` reaches existing installs (a role the engine
      can't resolve is a posting-time crash).

    Concurrency: two racing first-seeds both insert system rows, so the
    ``uq_gl_accounts_active_system_role`` partial unique index kills one
    transaction entirely (non-system rows roll back with it).
    """
    existing = session.scalars(
        select(GlAccount).where(GlAccount.company_id == company_id)
    ).all()
    first_boot = not existing
    have_roles = {a.role for a in existing if a.role is not None}

    inserted = 0
    for code, name, type_, role, is_system in DEFAULT_COA:
        if role is not None:
            if role in have_roles:
                continue
        elif not first_boot:
            continue
        session.add(
            GlAccount(
                code=code,
                name=name,
                type=type_,
                role=role,
                is_system=is_system,
                company_id=company_id,
            )
        )
        inserted += 1

    if inserted:
        session.flush()
    return inserted


def resolve_role_account(session: Session, company_id: str, role: str) -> GlAccount:
    """Return the single active system account owning ``role`` — the only way
    the engine turns a role into an account. Loud on every failure mode:
    unknown role, unseeded CoA, deactivated owner, ambiguous duplicates.
    """
    if role not in ALL_ROLES:
        raise LedgerConfigError(f"unknown ledger account role {role!r}")

    accounts = session.scalars(
        select(GlAccount).where(
            GlAccount.company_id == company_id,
            GlAccount.role == role,
            GlAccount.is_system.is_(True),
            GlAccount.active.is_(True),
        )
    ).all()

    if not accounts:
        raise LedgerConfigError(
            f"no active system account owns role {role!r} — CoA not seeded, "
            "or the owning account was deactivated without a replacement"
        )
    if len(accounts) > 1:
        codes = ", ".join(sorted(a.code for a in accounts))
        raise LedgerConfigError(
            f"role {role!r} is ambiguous — {len(accounts)} active system "
            f"accounts claim it ({codes}); fix on the Accounting Settings page"
        )
    return accounts[0]
