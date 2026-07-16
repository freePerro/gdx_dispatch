"""Cutover backfill (S10, spec §5.7 / plan §S10).

Three idempotent phases, run by ``tools/gl_backfill.py`` in the §11 rollout
order:

1. **opening** (flag may still be OFF — the rollout posts and reconciles
   opening balances BEFORE the flip): per open pre-cutover invoice, post the
   P8 anchor (AR debit / 3950 credit, ``opening_balance_cents`` formula), and
   ensure a period lock at ``cutover − 1 day`` so nothing back-posts into the
   pre-ledger era.
2. **replay** (flag must be ON): re-drive every post-cutover money event
   through the SAME builders the live chokepoint uses —
   ``repost_invoice_issuance`` / ``resettle_invoice_payments`` /
   ``post_credit_memo``·``post_refund``·``post_credit_application`` /
   ``repost_expense`` — so a backfill replay lands on keys identical to what
   live posting would have minted (§5.6: same state → same content → same
   key). Events the period lock refuses are collected, not crashed on.
3. **report**: per-invoice AR reconciliation — operational ``balance_due``
   vs the ledger's attributable AR — plus the GL-wide AR total. This is the
   §5.7 hand-check against QBO's aging; discrepancies are resolved by hand
   before the flag flips.

Everything here is re-runnable by construction: posting is content-keyed
idempotent, the lock ensure is existence-checked, and the report is
read-only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.ledger import service as ledger_service
from gdx_dispatch.modules.ledger.coa import LedgerConfigError, resolve_role_account
from gdx_dispatch.modules.ledger.engine import PeriodLockedError
from gdx_dispatch.modules.ledger.models import (
    ROLE_AR,
    GlJournalEntry,
    GlJournalLine,
    GlPeriodLock,
)
from gdx_dispatch.modules.ledger.money import to_cents
from gdx_dispatch.modules.ledger.rules import (
    ISSUED_STATUSES,
    ExpenseCompositionError,
    IssuanceCompositionError,
    _created_date,
    _dec,
    _effective_at,
    _existed_at_cutover,
    has_opening_anchor,
    invoice_ar_balance_cents,
    opening_balance_cents,
    post_credit_application,
    post_credit_memo,
    post_opening_balance,
    post_refund,
    pre_cutover_era,
    repost_expense,
    repost_invoice_issuance,
    resettle_invoice_payments,
)

log = logging.getLogger(__name__)

ACTOR = "gl_backfill"


@dataclass
class PhaseResult:
    posted: int = 0  # events processed (idempotent replays count — see entries_created)
    skipped: int = 0
    entries_created: int = 0  # NEW journal entries this run actually minted
    locked: list[str] = field(default_factory=list)  # period-lock refusals
    refused: list[str] = field(default_factory=list)  # data that won't reconcile
    rows: list[dict] = field(default_factory=list)


def _entry_count(session: Session, company_id: str) -> int:
    from sqlalchemy import func

    return session.scalar(
        select(func.count()).select_from(GlJournalEntry).where(
            GlJournalEntry.company_id == company_id
        )
    ) or 0


def _require_cutover(session: Session, company_id: str) -> date:
    settings = ledger_service.get_gl_settings(session, company_id)
    cutover = settings.cutover_month if settings else None
    if cutover is None:
        raise LedgerConfigError(
            "cutover_month is not set — plan the cutover on the Accounting "
            "Settings page first"
        )
    return cutover


def ensure_cutover_lock(session: Session, company_id: str, cutover: date) -> bool:
    """Lock every period before the cutover date. Idempotent: no-op when an
    equal-or-later lock already exists. The P8 entries themselves post AT the
    cutover date, which stays open."""
    lock_through = cutover - timedelta(days=1)
    latest = session.scalar(
        select(GlPeriodLock.lock_date)
        .where(GlPeriodLock.company_id == company_id)
        .order_by(GlPeriodLock.lock_date.desc())
        .limit(1)
    )
    if latest is not None and latest >= lock_through:
        return False
    session.add(
        GlPeriodLock(
            lock_date=lock_through,
            note=f"cutover lock — pre-ledger era closed through {lock_through}",
            created_by=ACTOR,
            company_id=company_id,
        )
    )
    session.flush()
    return True


def _company_invoices(session: Session, company_id: str):
    from gdx_dispatch.models.tenant_models import Invoice

    return session.scalars(
        select(Invoice).where(
            Invoice.company_id == company_id,
            Invoice.deleted_at.is_(None),
        )
    ).all()


def run_opening(session: Session, company_id: str) -> PhaseResult:
    """Phase 1 — P8 anchors for every open pre-cutover invoice + the era
    lock. Runs with the posting flag in either state (§11 posts these before
    the flip). Skips drafts (not receivables) and voids (net-zero either
    way — see spec §5.7 / the S10 PR discussion)."""
    cutover = _require_cutover(session, company_id)
    result = PhaseResult()
    if ensure_cutover_lock(session, company_id, cutover):
        result.rows.append({"event": "period_lock", "through": str(cutover - timedelta(days=1))})

    for invoice in _company_invoices(session, company_id):
        if invoice.status not in ISSUED_STATUSES:
            result.skipped += 1
            continue
        if _effective_at(invoice) >= cutover:
            result.skipped += 1
            continue
        opening = opening_balance_cents(session, invoice, cutover)
        row = {
            "invoice_number": invoice.invoice_number,
            "invoice_id": str(invoice.id),
            "customer_id": str(invoice.customer_id) if invoice.customer_id else None,
            "effective_at": str(_effective_at(invoice)),
            "opening_cents": opening,
        }
        if opening <= 0:
            # settled (or over-credited) at cutover — nothing open to anchor.
            # opening < 0 = the customer was OWED at cutover; that credit
            # stays in the QBO-era books (no 2300 minted) — hand-review.
            row["posted"] = False
            row["overpaid_at_cutover"] = opening < 0
            result.skipped += 1
        else:
            entry = post_opening_balance(session, invoice, actor=ACTOR)
            row["posted"] = entry is not None
            if entry is not None:
                result.posted += 1
        result.rows.append(row)
    return result


def run_replay(session: Session, company_id: str) -> PhaseResult:
    """Phase 2 — replay post-cutover money events through the live builders.
    Requires the flag ON (the rules it drives are flag-gated; a silent
    no-op replay would read as success)."""
    if not ledger_service.ledger_posting_enabled(session, company_id):
        raise LedgerConfigError(
            "ledger_posting_enabled is off — replay re-drives the flag-gated "
            "posting rules and would silently do nothing. Flip the flag "
            "(Accounting Settings → enable posting) first."
        )
    cutover = _require_cutover(session, company_id)
    from gdx_dispatch.models.tenant_models import Expense, InvoiceAdjustment

    result = PhaseResult()
    entries_at_start = _entry_count(session, company_id)

    invoices = [i for i in _company_invoices(session, company_id) if i.status != "void"]
    by_id = {i.id: i for i in invoices}

    # Issuances (post-cutover invoices; era invoices anchor inside repost).
    for invoice in invoices:
        if invoice.status not in ISSUED_STATUSES:
            continue
        try:
            repost_invoice_issuance(session, invoice, actor=ACTOR)
            result.posted += 1
        except PeriodLockedError as exc:
            result.locked.append(f"invoice {invoice.invoice_number}: {exc}")
        except IssuanceCompositionError as exc:
            # Legacy rows whose total ≠ lines + tax beyond rounding: refuse
            # THIS invoice into the report, never the whole replay.
            result.refused.append(f"invoice {invoice.invoice_number}: {exc}")

    # Adjustments — dispatch by kind; opening-era adjustments on anchored
    # invoices are inside the P8 amount and stay off the ledger.
    poster_by_kind = {
        "credit_memo": post_credit_memo,
        "refund": post_refund,
        "credit_applied": post_credit_application,
    }
    adjustments = (
        session.scalars(
            select(InvoiceAdjustment).where(
                InvoiceAdjustment.invoice_id.in_(list(by_id))
            )
        ).all()
        if by_id
        else []
    )
    for adjustment in adjustments:
        invoice = by_id.get(adjustment.invoice_id)
        if invoice is None:
            continue
        if pre_cutover_era(session, invoice) and _existed_at_cutover(adjustment, cutover):
            # inside the opening amount (or the settledness) — stays off-ledger
            result.skipped += 1
            continue
        poster = poster_by_kind.get(adjustment.kind)
        if poster is None:
            result.skipped += 1
            continue
        try:
            poster(session, adjustment, invoice, actor=ACTOR)
            result.posted += 1
        except PeriodLockedError as exc:
            result.locked.append(
                f"adjustment {adjustment.id} on {invoice.invoice_number}: {exc}"
            )

    # Payments — resettle per invoice reposts every payment at current state
    # (and skips opening-era rows on anchored invoices).
    for invoice in invoices:
        try:
            resettle_invoice_payments(session, invoice, actor=ACTOR)
        except PeriodLockedError as exc:
            result.locked.append(f"payments on {invoice.invoice_number}: {exc}")

    # Expenses — post-cutover dated only; pre-cutover expense history stays
    # off-ledger (P8 is AR-only; spec §5.7).
    expenses = session.scalars(
        select(Expense).where(Expense.company_id == company_id)
    ).all()
    for expense in expenses:
        when = expense.date or _created_date(expense)
        if when is None or when < cutover:
            result.skipped += 1
            continue
        try:
            repost_expense(session, expense, actor=ACTOR)
            result.posted += 1
        except PeriodLockedError as exc:
            result.locked.append(f"expense {expense.id}: {exc}")
        except ExpenseCompositionError as exc:
            result.refused.append(f"expense {expense.id}: {exc}")

    result.entries_created = _entry_count(session, company_id) - entries_at_start
    return result


def gl_ar_balance_cents(session: Session, company_id: str) -> int:
    """The AR account's total balance across every entry (reversals net out)."""
    ar_account = resolve_role_account(session, company_id, ROLE_AR)
    rows = session.execute(
        select(GlJournalLine.amount_cents)
        .join(GlJournalEntry, GlJournalLine.entry_id == GlJournalEntry.id)
        .where(
            GlJournalEntry.company_id == company_id,
            GlJournalLine.account_id == ar_account.id,
        )
    ).all()
    return sum(amount for (amount,) in rows)


def reconciliation_report(session: Session, company_id: str) -> dict:
    """Per-invoice AR: operational ``balance_due`` vs ledger attribution.
    The §5.7 hand-check — compare the per-invoice rows against QBO's AR
    aging before flipping the flag; the totals row is the running monthly
    check afterwards."""
    rows = []
    op_total = 0
    gl_attributed = 0
    legacy_credit_suspects = []
    for invoice in _company_invoices(session, company_id):
        if invoice.status not in ISSUED_STATUSES:
            continue
        op_cents = to_cents(_dec(invoice.balance_due))
        gl_cents = invoice_ar_balance_cents(session, invoice)
        op_total += op_cents
        gl_attributed += gl_cents
        # Pre-GL credit memos lived as amount_paid mutations — invisible to
        # BOTH the opening formula and balance_due recomputation, so their
        # delta reads zero here (self-consistent, not correct). Surface every
        # invoice carrying legacy amount_paid so the QBO aging hand-check
        # knows exactly which rows deserve a hard look.
        legacy_paid = to_cents(_dec(getattr(invoice, "amount_paid", None)))
        if legacy_paid:
            legacy_credit_suspects.append(
                {
                    "invoice_number": invoice.invoice_number,
                    "amount_paid_cents": legacy_paid,
                }
            )
        if op_cents or gl_cents:
            rows.append(
                {
                    "invoice_number": invoice.invoice_number,
                    "status": invoice.status,
                    "operational_cents": op_cents,
                    "gl_cents": gl_cents,
                    "delta_cents": op_cents - gl_cents,
                    "anchored": has_opening_anchor(session, invoice),
                }
            )
    mismatches = [r for r in rows if r["delta_cents"] != 0]
    return {
        "rows": rows,
        "mismatches": mismatches,
        "legacy_credit_suspects": legacy_credit_suspects,
        "totals": {
            "operational_ar_cents": op_total,
            "gl_attributed_ar_cents": gl_attributed,
            "gl_ar_account_cents": gl_ar_balance_cents(session, company_id),
        },
    }
