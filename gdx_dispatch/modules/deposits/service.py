"""Deposit invoices — downpayments collected at estimate acceptance.

Why an *invoice* (adversarial audit, 2026-07-23): Payment.invoice_id is NOT
NULL and every GL posting is keyed payment→invoice, so a deposit MUST land
on an invoice. billing_type='deposit' has been latent in the enum since the
original schema; this module is the first thing that sets it.

The three rules that keep the money right:

1. **A deposit invoice never "bills" the job.** core/billing_predicates.py
   excludes billing_type='deposit', so the job stays in Ready-for-Billing
   and the mobile Bill button stays visible for the FINAL invoice.
2. **The final invoice nets the deposit with a negative line.** GL issuance
   (modules/ledger/rules.py build_issuance_lines) turns a negative
   line_total into a revenue DEBIT, so deposit revenue + final revenue net
   to exactly the job's true total — no 150% double-count. Both the deposit
   line and the netting line carry DEPOSIT_CATEGORY, so even the
   unmapped-category fallback account nets to zero. Both are taxable=False:
   tax is charged once, on the final invoice's real lines.
3. **An unpaid deposit remainder is superseded at final-invoice time** via
   a credit memo (existing GL S7 machinery). Without this, an
   accept-then-abandon customer would owe deposit + full total (150%).
"""
from __future__ import annotations

import logging
import secrets
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import (
    Invoice,
    InvoiceAdjustment,
    InvoiceLine,
    Payment,
)

log = logging.getLogger(__name__)

DEPOSIT_CATEGORY = "Deposit"


class DepositError(ValueError):
    """A deposit request that must not become an invoice (bad amount, no
    customer). Callers translate to a 4xx or a skipped-with-reason field —
    never a 500, and never a failed acceptance."""


def _to_f(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def deposit_summary(invoice: Invoice) -> dict:
    """Client-facing shape for a deposit invoice, shared by all accept
    responses (office / portal / mobile). pay_url is the E2E-verified public
    Stripe Elements page — None when Stripe/base-URL isn't configured, and
    the frontends degrade to showing the invoice number."""
    from gdx_dispatch.core.payments import public_pay_url

    return {
        "invoice_id": str(invoice.id),
        "invoice_number": invoice.invoice_number,
        "amount": _to_f(invoice.total),
        "balance_due": _to_f(invoice.balance_due),
        "status": invoice.status,
        "pay_url": public_pay_url(invoice.public_token),
    }


def find_deposit_invoice_for_estimate(db: Session, estimate_id) -> Invoice | None:
    """The live (non-void, non-deleted) deposit invoice born from this
    estimate, if one exists. Accept endpoints use this for idempotency."""
    return db.execute(
        select(Invoice).where(
            Invoice.estimate_id == estimate_id,
            Invoice.billing_type == "deposit",
            Invoice.deleted_at.is_(None),
            Invoice.status != "void",
        ).order_by(Invoice.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def create_deposit_invoice(
    db: Session,
    *,
    estimate,
    amount: float,
    tenant_id: str,
    actor: str,
    source: str,
) -> Invoice:
    """Create + issue (status 'sent') a deposit invoice for an accepted
    estimate. Commits. Idempotent per estimate: an existing live deposit
    invoice is returned instead of minting a second one (double-tapped
    accept buttons, office accept after mobile accept).

    Raises DepositError for requests that must not become invoices; the
    caller decides whether that's a 4xx (explicit office request) or a
    logged skip (automatic portal/mobile flow).
    """
    # Late imports: the single money/recalc/numbering truths live in
    # routers.invoices; importing at call time avoids modules←routers
    # import cycles at module load (same pattern as core/payments.py).
    from gdx_dispatch.core.audit import log_audit_event_sync
    from gdx_dispatch.modules.ledger.service import transition_invoice_status
    from gdx_dispatch.routers.invoices import (
        _money,
        _next_invoice_number,
        _recalculate_invoice,
    )

    existing = find_deposit_invoice_for_estimate(db, estimate.id)
    if existing is not None:
        return existing

    if estimate.customer_id is None:
        raise DepositError("estimate has no customer — a deposit invoice needs one")
    amount_dec = _money(Decimal(str(amount or 0)))
    if amount_dec <= 0:
        raise DepositError("deposit amount must be greater than zero")

    # Sanity cap: never invoice a deposit larger than the estimate itself.
    # compute_estimate_totals is the canonical total (tiers/discount/tax);
    # a $0 estimate (no lines yet) skips the cap — the operator knows better.
    try:
        from gdx_dispatch.modules.proposals.totals import compute_estimate_totals

        est_total = _to_f(compute_estimate_totals(estimate, db)["total"])
    except Exception:
        log.exception("deposit_estimate_total_failed estimate=%s", estimate.id)
        est_total = _to_f(estimate.total)
    if est_total > 0 and float(amount_dec) > est_total + 0.005:
        raise DepositError(
            f"deposit ({float(amount_dec):.2f}) exceeds the estimate total ({est_total:.2f})"
        )

    now = datetime.now(UTC)
    company_id = str(tenant_id or estimate.company_id or "")
    invoice = Invoice(
        id=uuid4(),
        job_id=estimate.job_id,
        estimate_id=estimate.id,
        invoice_number=_next_invoice_number(db),
        billing_type="deposit",
        sequence_number=1,
        subtotal=amount_dec,
        tax_rate=None,
        tax_amount=0,
        total=amount_dec,
        balance_due=amount_dec,
        status="draft",
        # Due on receipt — a deposit is the "before we order doors" money.
        invoice_date=date.today(),
        due_date=date.today(),
        notes=f"Deposit for Estimate {estimate.estimate_number}",
        public_token=secrets.token_urlsafe(48)[:64],
        locked=False,
        customer_id=estimate.customer_id,
        company_id=company_id,
        created_at=now,
    )
    db.add(invoice)
    db.flush()
    db.add(
        InvoiceLine(
            id=uuid4(),
            company_id=company_id,
            invoice_id=invoice.id,
            description=f"Deposit — Estimate {estimate.estimate_number}"[:500],
            quantity=1,
            unit_price=amount_dec,
            line_total=amount_dec,
            # taxable=False + tax_rate=None: tax is charged once, on the
            # final invoice's real lines (MN construction contracts have no
            # customer sales tax anyway; this keeps taxed tenants correct too).
            taxable=False,
            category=DEPOSIT_CATEGORY,
            sort_order=1,
            created_at=now,
        )
    )
    db.flush()
    _recalculate_invoice(invoice, db)
    # Issue through the chokepoint so P1 posts when the ledger flag is on.
    # sent_at deliberately NOT stamped — since PR #192 it means "an email
    # was actually delivered", and no email goes out here.
    transition_invoice_status(db, invoice, "sent", actor=actor)
    db.commit()
    db.refresh(invoice)

    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=actor,
        action="deposit_invoice_created",
        entity_type="invoice",
        entity_id=str(invoice.id),
        details={
            "estimate_id": str(estimate.id),
            "estimate_number": estimate.estimate_number,
            "invoice_number": invoice.invoice_number,
            "amount": float(amount_dec),
            "source": source,
        },
    )
    db.commit()
    return invoice


def adopt_orphan_deposit_invoices(db: Session, estimate, job_id) -> int:
    """Backfill job_id on deposit invoices born from this estimate before it
    had a job (mobile accept creates no job). Called by
    _create_job_from_estimate inside its transaction; no commit here."""
    from sqlalchemy import update

    rows = db.execute(
        update(Invoice)
        .where(
            Invoice.estimate_id == estimate.id,
            Invoice.billing_type == "deposit",
            Invoice.job_id.is_(None),
            Invoice.deleted_at.is_(None),
        )
        .values(job_id=job_id)
        .returning(Invoice.id)
    ).scalars().all()
    return len(rows)


def apply_deposits_to_final(db: Session, invoice: Invoice, *, actor: str) -> dict | None:
    """Net this job's deposit invoices into a freshly-created final/standard
    invoice. Adds ONE negative 'Less deposit paid' line for the paid portion
    and supersedes any unpaid deposit remainder with a credit memo.

    Contract: caller creates the invoice + its lines first, calls this, then
    runs its own total recompute (canonical create → _recalculate_invoice;
    one-click → hand-adjusted totals, mirrored from its CO/parts pattern).
    No commit here — everything lands or rolls back with the caller.

    Returns a summary dict, or None when there is nothing to do.
    """
    from gdx_dispatch.modules.ledger.rules import (
        post_credit_memo,
        resettle_invoice_payments,
        reverse_invoice_adjustments,
        settle_opening_on_void,
    )
    from gdx_dispatch.modules.ledger.service import transition_invoice_status
    from gdx_dispatch.routers.invoices import _money, _recalculate_invoice

    if (invoice.billing_type or "") == "deposit":
        return None
    match = []
    if invoice.job_id is not None:
        match.append(Invoice.job_id == invoice.job_id)
    if getattr(invoice, "estimate_id", None) is not None:
        match.append(Invoice.estimate_id == invoice.estimate_id)
    if not match:
        return None

    deposits = db.execute(
        select(Invoice).where(
            or_(*match),
            Invoice.billing_type == "deposit",
            Invoice.id != invoice.id,
            Invoice.deleted_at.is_(None),
            Invoice.status != "void",
        ).order_by(Invoice.created_at.asc())
    ).scalars().all()
    if not deposits:
        return None

    # Double-application guard: if another live final invoice on this job
    # already carries a deposit-netting line (force-created second final),
    # applying again would subtract the same deposit twice. Which deposit a
    # netting line covered is not modeled, so the safe answer is: don't.
    if invoice.job_id is not None:
        prior = db.execute(
            select(Invoice.invoice_number)
            .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
            .where(
                Invoice.job_id == invoice.job_id,
                Invoice.id != invoice.id,
                Invoice.billing_type != "deposit",
                Invoice.deleted_at.is_(None),
                Invoice.status != "void",
                InvoiceLine.deleted_at.is_(None),
                InvoiceLine.category == DEPOSIT_CATEGORY,
                InvoiceLine.line_total < 0,
            )
            .limit(1)
        ).scalar_one_or_none()
        if prior:
            log.warning(
                "deposit_already_applied job=%s prior_invoice=%s new_invoice=%s",
                invoice.job_id, prior, invoice.invoice_number,
            )
            return {"skipped": f"deposit already applied on {prior}"}

    total_paid = 0.0
    paid_sources: list[str] = []
    superseded: list[str] = []
    voided: list[str] = []
    for dep in deposits:
        _recalculate_invoice(dep, db)  # true-up before reading balance_due
        paid = _to_f(
            db.execute(
                select(func.sum(Payment.amount)).where(
                    Payment.invoice_id == dep.id,
                    Payment.voided_at.is_(None),
                )
            ).scalar_one_or_none()
        )
        if paid > 0.009:
            total_paid += paid
            paid_sources.append(dep.invoice_number)
        remainder = _to_f(dep.balance_due)
        if remainder > 0.009 and dep.status in ("sent", "overdue"):
            if paid <= 0.009:
                # Wholly-unpaid abandoned deposit → VOID, mirroring the
                # /void endpoint (transition + adjustment reversal + opening
                # settle + zero balance). Implementation-audit catch: the
                # credit-memo settle flips balance-0 invoices to "paid" —
                # a never-paid deposit showing "paid" in the portal, with
                # the /pay page thanking the customer for money that never
                # moved, and record_payment happily accepting a late check
                # onto a settled bill. Void reads honestly as cancelled AND
                # blocks late payments at the existing void guard.
                transition_invoice_status(db, dep, "void", actor=actor)
                reverse_invoice_adjustments(db, dep, actor=actor)
                settle_opening_on_void(db, dep, actor=actor)
                dep.balance_due = _money(Decimal("0"))
                voided.append(dep.invoice_number)
            else:
                # Partially-paid: the payment history must survive, so void
                # is off the table — credit-memo the remainder. The paid
                # portion nets on the final below; record_payment refuses
                # further money on a superseded deposit (409 → final).
                adj = InvoiceAdjustment(
                    invoice_id=dep.id,
                    kind="credit_memo",
                    amount=_money(Decimal(str(remainder))),
                    reason=f"Deposit superseded by {invoice.invoice_number}"[:200],
                    created_by=actor,
                    company_id=dep.company_id,
                )
                db.add(adj)
                db.flush()
                post_credit_memo(db, adj, dep, actor=actor)
                resettle_invoice_payments(db, dep, actor=actor)
                _recalculate_invoice(dep, db)
                superseded.append(dep.invoice_number)
        # A DRAFT deposit with a balance is edited or deleted, not credited
        # (mirrors issue_credit_memo's draft refusal) — leave it alone.

    result = {
        "deposit_paid_applied": 0.0,
        "deposit_unapplied": 0.0,
        "superseded": superseded,
        "voided": voided,
        "sources": paid_sources,
    }
    if total_paid <= 0.009:
        return result

    db.flush()
    line_sum = _to_f(
        db.execute(
            select(func.sum(InvoiceLine.line_total)).where(
                InvoiceLine.invoice_id == invoice.id,
                InvoiceLine.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
    )
    # Cap: the netting line must not push the final invoice negative. A paid
    # deposit larger than the final's lines leaves the excess unapplied —
    # returned in the create response (toasted on /billing/new) and logged,
    # resolved by a human (refund or credit memo), never silently invented
    # as negative AR. Cap is pre-tax line-sum, deliberately conservative:
    # a deposit landing between line-sum and line-sum+tax reports a small
    # unapplied excess instead of risking a negative-total invoice.
    applied = round(min(total_paid, max(line_sum, 0.0)), 2)
    if applied > 0:
        max_sort = db.execute(
            select(func.max(InvoiceLine.sort_order)).where(
                InvoiceLine.invoice_id == invoice.id
            )
        ).scalar_one_or_none() or 0
        db.add(
            InvoiceLine(
                id=uuid4(),
                company_id=invoice.company_id,
                invoice_id=invoice.id,
                description=("Less deposit paid — " + ", ".join(paid_sources))[:500],
                quantity=1,
                unit_price=_money(Decimal(str(-applied))),
                line_total=_money(Decimal(str(-applied))),
                taxable=False,
                category=DEPOSIT_CATEGORY,
                sort_order=int(max_sort) + 1,
            )
        )
        db.flush()
    result["deposit_paid_applied"] = applied
    result["deposit_unapplied"] = round(total_paid - applied, 2)
    if result["deposit_unapplied"] > 0:
        log.warning(
            "deposit_unapplied_excess invoice=%s amount=%.2f",
            invoice.invoice_number, result["deposit_unapplied"],
        )
    return result
