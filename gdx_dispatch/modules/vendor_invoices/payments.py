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
    """Invalid payment operation — surfaces as a 4xx, never a 500."""


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
        raise PaymentError(f"invalid payment source {source!r}")
    # Lock the bill row before reading the balance: the open-balance cap is
    # a read-then-insert invariant, and two concurrent recorders (office
    # double-click + a bank-match confirm) could both pass an unlocked
    # check and overpay the bill. Postgres honors FOR UPDATE; SQLite
    # (tests) degrades to a refresh — same pattern as confirm_line.
    db.refresh(invoice, with_for_update=True)
    if invoice.status == STATUS_VOID:
        raise PaymentError("bill is void — reopen it before recording payments")
    amount = Decimal(amount)
    if amount <= 0:
        raise PaymentError("payment amount must be positive")
    balance = open_balance(db, invoice)
    if amount > balance:
        raise PaymentError(
            f"payment {amount} exceeds the bill's open balance {balance} — "
            "void the wrong record first if reality disagrees"
        )
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
            raise PaymentError(
                "this payment was recorded by a confirmed bank match — "
                "unconfirm the match instead of voiding the payment"
            )
    payment.voided_at = _now()
    payment.voided_by = voided_by
    db.flush()
    invoice = db.get(VendorInvoice, payment.vendor_invoice_id)
    if invoice is not None:
        recompute_status(db, invoice)
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
