"""Vendor bill payments — record, void, and the derived paid status.

Books-convergence Track 1 (docs/design/books-convergence-plan.md §Track 1,
audit conditions applied). The rules, in one place:

- ``status`` is WRITE-THROUGH DERIVED: ``recompute_status`` is the only
  writer of 'paid'/'open' (void stays a header-level office assertion and is
  sticky — payments never resurrect a voided bill). The raw status PATCH no
  longer accepts 'paid'; the payment endpoints are the single writer.
- Payments are void-only: never edited, never deleted. A mistake is voided
  and the status recomputes.
- The open-balance cap (plan-audit BLOCKER 1): AUTO paths (bank-match
  confirm) refuse to record beyond the open balance — two evidence streams
  witnessing the same settlement cannot double-book it. The manual endpoint
  enforces the same cap; the office voids the wrong record first if reality
  disagrees.
- Match-created payments (``match_id`` set) refuse direct voiding while
  their match stays confirmed — unconfirm is the ceremony (mirrors the
  statement-import void rule). ``statement_matching`` calls
  ``void_payment(..., via_unconfirm=True)`` from the unconfirm path.

Partial payments are legal and expressed as derived numbers
(``paid_total``/``open_balance``/``is_partial``) — never as new status
strings; every VALID_STATUSES consumer keeps working (plan-audit MUST-FIX 7).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.vendor_invoices.models import (
    STATUS_OPEN,
    STATUS_PAID,
    STATUS_VOID,
    VALID_PAYMENT_SOURCES,
    VendorBillPayment,
    VendorInvoice,
)


class PaymentError(ValueError):
    """Invalid payment operation — surfaces as a 4xx, never a 500.

    Carries a ``code`` so responses use the constant-table lookup below
    (the repo's CodeQL-clean pattern from the deposits module: dict-lookup
    by code breaks the exception→response taint, so a future wrapper can
    never leak foreign exception text through these flows)."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(PAYMENT_REFUSAL_MESSAGES.get(code, code))


# Operator-facing refusal text — constant table, never exception-derived.
PAYMENT_REFUSAL_MESSAGES: dict[str, str] = {
    "invalid_source": "invalid payment source",
    "bill_void": "bill is void — reopen it before recording payments",
    "amount_not_positive": "payment amount must be positive",
    "over_open_balance": (
        "payment exceeds the bill's open balance — void the wrong record "
        "first if reality disagrees"
    ),
    "match_void_ceremony": (
        "this payment was recorded by a confirmed bank match — unconfirm "
        "the match instead of voiding the payment"
    ),
}


def payment_refusal_message(exc: PaymentError) -> str:
    """Response text for a PaymentError: constant-table lookup by code."""
    return PAYMENT_REFUSAL_MESSAGES.get(getattr(exc, "code", ""), "invalid payment operation")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def live_payments(db: Session, invoice: VendorInvoice) -> list[VendorBillPayment]:
    return list(
        db.scalars(
            select(VendorBillPayment)
            .where(
                VendorBillPayment.vendor_invoice_id == invoice.id,
                VendorBillPayment.voided_at.is_(None),
            )
            .order_by(VendorBillPayment.created_at)
        ).all()
    )


def paid_total(db: Session, invoice: VendorInvoice) -> Decimal:
    return sum((p.amount for p in live_payments(db, invoice)), Decimal("0.00"))


def open_balance(db: Session, invoice: VendorInvoice) -> Decimal:
    """total − Σ live payments. Never negative in practice (the cap refuses
    over-recording), but not clamped — a negative here means an invariant
    broke and hiding it would be worse."""
    return (invoice.total or Decimal("0.00")) - paid_total(db, invoice)


def recompute_status(db: Session, invoice: VendorInvoice) -> str:
    """Write-through derivation. Void is sticky (an office assertion about
    the BILL, independent of any money that moved against it)."""
    if invoice.status == STATUS_VOID:
        return invoice.status
    total = invoice.total or Decimal("0.00")
    covered = total > 0 and paid_total(db, invoice) >= total
    invoice.status = STATUS_PAID if covered else STATUS_OPEN
    return invoice.status


def record_payment(
    db: Session,
    invoice: VendorInvoice,
    *,
    amount: Decimal,
    paid_date: date | None,
    source: str,
    reference: str | None = None,
    match_id: UUID | None = None,
    statement_id: UUID | None = None,
    created_by: str | None = None,
) -> VendorBillPayment:
    """Insert one payment and recompute the bill status. Flushes; the caller
    owns the commit (the bank-match confirm runs this inside the SAME
    transaction as its status flip — plan-audit BLOCKER 3)."""
    if source not in VALID_PAYMENT_SOURCES:
        raise PaymentError("invalid_source")
    # Lock the bill row before reading the balance: the open-balance cap is
    # a read-then-insert invariant, and two concurrent recorders (office
    # double-click + a bank-match confirm) could both pass an unlocked
    # check and overpay the bill. Postgres honors FOR UPDATE; SQLite
    # (tests) degrades to a refresh — same pattern as confirm_line.
    db.refresh(invoice, with_for_update=True)
    if invoice.status == STATUS_VOID:
        raise PaymentError("bill_void")
    amount = Decimal(amount)
    if amount <= 0:
        raise PaymentError("amount_not_positive")
    balance = open_balance(db, invoice)
    if amount > balance:
        raise PaymentError("over_open_balance")
    payment = VendorBillPayment(
        vendor_invoice_id=invoice.id,
        amount=amount,
        paid_date=paid_date,
        source=source,
        reference=reference,
        match_id=match_id,
        statement_id=statement_id,
        created_by=created_by,
    )
    db.add(payment)
    db.flush()
    recompute_status(db, invoice)
    sync_expense_dates(db, invoice)
    return payment


def void_payment(
    db: Session,
    payment: VendorBillPayment,
    *,
    voided_by: str | None = None,
    via_unconfirm: bool = False,
) -> VendorBillPayment:
    """Void one payment and recompute. Idempotent (already-voided no-ops —
    the unconfirm path must never double-reverse). Match-created payments
    demand the unconfirm ceremony unless this IS the unconfirm path."""
    if payment.voided_at is not None:
        return payment
    if payment.match_id is not None and not via_unconfirm:
        from gdx_dispatch.modules.bank_feeds.statement_models import (
            MATCH_CONFIRMED,
            BankMatch,
        )

        match = db.get(BankMatch, payment.match_id)
        if match is not None and match.status == MATCH_CONFIRMED:
            raise PaymentError("match_void_ceremony")
    payment.voided_at = _now()
    payment.voided_by = voided_by
    db.flush()
    invoice = db.get(VendorInvoice, payment.vendor_invoice_id)
    if invoice is not None:
        recompute_status(db, invoice)
        sync_expense_dates(db, invoice)
    return payment


def payment_summary(db: Session, invoice: VendorInvoice) -> dict:
    """Derived money facts for API payloads — partiality is data, not a
    status string."""
    total = invoice.total or Decimal("0.00")
    paid = paid_total(db, invoice)
    return {
        "paid_total": float(paid),
        "open_balance": float(total - paid),
        "is_partial": bool(paid > 0 and paid < total),
    }


# ── cash-basis expense dating (Doug, 2026-08-14: "payment date for cash
# basis" — the CPA timing question the Track-1 audits flagged) ──────────


def settled_date(db: Session, invoice: VendorInvoice) -> date | None:
    """The cash-basis recognition date for a SETTLED bill: the latest live
    payment's paid_date (the payment that completed settlement). None when
    the bill isn't fully paid, or when the settling payment carries no date
    (the migration backfill — date unknown means exactly that, so those
    bills keep their invoice-date expenses rather than gaining a fiction)."""
    total = invoice.total or Decimal("0.00")
    payments = live_payments(db, invoice)
    if total <= 0 or sum((p.amount for p in payments), Decimal("0.00")) < total:
        return None
    dated = [p.paid_date for p in payments if p.paid_date is not None]
    if len(dated) != len(payments):
        return None
    return max(dated)


def effective_expense_date(db: Session, invoice: VendorInvoice) -> date:
    """What a vendor-bill expense should be dated TODAY: the settlement
    date when the bill is fully paid (cash basis — recognition when cash
    left, which for bank-match payments is the literal bank date), else the
    invoice date as the best-known placeholder until it settles."""
    settled = settled_date(db, invoice)
    if settled is not None:
        return settled
    return invoice.invoice_date or datetime.now(timezone.utc).date()


def sync_expense_dates(db: Session, invoice: VendorInvoice) -> int:
    """Re-date the bill's line-expenses to the current cash-basis date and
    repost them (flag-gated inside repost_expense; no-op while the ledger
    is dark). Called after every payment record/void so settlement moves
    the expense to the payment date and un-settlement moves it back.

    Only touches ``source='vendor_invoice'`` expenses reached through this
    bill's OWN lines — never a manually keyed or bank-line-created expense.
    Pre-cutover target dates skip the GL repost (era-by-date, same rule as
    the confirm path); the operational re-date still applies.
    """
    from gdx_dispatch.modules.vendor_invoices.models import VendorInvoiceLine

    target = effective_expense_date(db, invoice)
    expense_ids = [
        line.expense_id
        for line in db.scalars(
            select(VendorInvoiceLine).where(
                VendorInvoiceLine.vendor_invoice_id == invoice.id,
                VendorInvoiceLine.expense_id.is_not(None),
            )
        ).all()
    ]
    if not expense_ids:
        return 0

    from sqlalchemy import select as _select

    from gdx_dispatch.models.tenant_models import Expense
    from gdx_dispatch.modules.ledger import service as ledger_service
    from gdx_dispatch.modules.ledger.engine import PeriodLockedError, reverse_entry
    from gdx_dispatch.modules.ledger.models import (
        ENTRY_STATUS_POSTED,
        GlJournalEntry,
    )
    from gdx_dispatch.modules.ledger.rules import repost_expense

    def _posted_entries(expense):
        # reverses_entry_id filter (re-audit BLOCKER): reversal entries
        # inherit source_type/source_id and sit status-POSTED — without
        # excluding them, a second settle→void cycle would "reverse the
        # reversal" and re-assert the original amounts.
        return db.scalars(
            _select(GlJournalEntry).where(
                GlJournalEntry.company_id == expense.company_id,
                GlJournalEntry.source_type == "expense",
                GlJournalEntry.source_id == str(expense.id),
                GlJournalEntry.status == ENTRY_STATUS_POSTED,
                GlJournalEntry.reverses_entry_id.is_(None),
            )
        ).all()

    def _reverse_lock_tolerant(entry):
        """Reverse at the entry's own date; if that month is locked, use
        reverse_entry's documented escape hatch — post the reversal into the
        current open period instead (gate-audit BLOCKER 1: a locked month
        must never make settling its bills impossible)."""
        try:
            reverse_entry(db, entry)
        except PeriodLockedError:
            reverse_entry(db, entry, effective_at=datetime.now(timezone.utc).date())

    changed = 0
    cutover = None
    cutover_loaded = False
    for expense_id in expense_ids:
        expense = db.get(Expense, expense_id)
        if expense is None or expense.deleted_at is not None:
            continue
        if expense.source != "vendor_invoice" or expense.date == target:
            continue
        # The cash-basis rule OWNS this date for vendor_invoice expenses:
        # a hand-edited date on one of these rows is overwritten by the next
        # payment event on its bill (documented; the office edits the
        # payment record, not the expense date, to move recognition).
        prior = _posted_entries(expense)
        expense.date = target
        changed += 1
        if not cutover_loaded:
            settings = ledger_service.get_gl_settings(db, expense.company_id)
            cutover = settings.cutover_month if settings else None
            cutover_loaded = True
        if cutover is not None and target < cutover:
            # Era branch (gate-audit BLOCKER 2): the pre-cutover placeholder
            # belongs to the QBO-era books, so nothing fresh posts — but a
            # live entry from a prior settlement (posted at a post-cutover
            # date) must still unwind, or the bill reopens while the GL
            # keeps saying cash left.
            for entry in prior:
                _reverse_lock_tolerant(entry)
            continue
        try:
            repost_expense(db, expense)
        except PeriodLockedError:
            # repost posts-first-then-reverses. If the NEW entry landed and
            # the raise came from reversing the OLD one (its month locked),
            # finish the reversal at the target date. If nothing new landed,
            # the TARGET month itself is locked — a genuine refusal that
            # must reach the caller as such, not a 500.
            fresh = [
                e for e in _posted_entries(expense)
                if e.effective_at == target and all(e.id != p.id for p in prior)
            ]
            if not fresh:
                raise
            for entry in prior:
                if entry.status == ENTRY_STATUS_POSTED:
                    reverse_entry(db, entry, effective_at=target)
    if changed:
        db.flush()
    return changed
