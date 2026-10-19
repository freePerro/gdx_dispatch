"""The one invoice void (GL S5, spec §5.1).

Extracted from `routers/invoices.py::void_invoice` (GDXA-1848) so a second
caller can perform the same void instead of a lookalike. GDXA-1847: the MCP
`invoices.void` tool fetched the invoice, returned ``{"voided": ...}`` and
changed nothing; wiring it to this function is that issue's ai-mcp child, not
this change. Every effect of a void is performed here:

- refuse while live (unvoided) payments exist — voiding a bill while keeping
  its money would silently orphan the cash;
- the status transition (which reverses the live P1 entry when ledger posting
  is on), the adjustment reversal and the opening-AR settlement;
- the part, change-order and day-row releases;
- cancelling an active payment plan;
- the ``invoice_voided`` audit row, staged into the same commit;
- the stale-intent sweep, enqueued only after that commit.

The caller owns authorization; this function owns the money.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.models.tenant_models import Invoice, JobPartNeeded, Payment
from gdx_dispatch.modules.ledger.engine import PeriodLockedError
from gdx_dispatch.modules.ledger.rules import reverse_invoice_adjustments, settle_opening_on_void
from gdx_dispatch.modules.ledger.service import transition_invoice_status
from gdx_dispatch.tasks.stale_intent_sweep import enqueue_stale_intent_sweep


class InvoiceVoidRefused(Exception):
    """The void did not happen and nothing was written by it.

    ``status_code`` is the HTTP status the route answers with (404 or 409);
    ``detail`` is the reason, worded for the operator.
    """

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class InvoiceVoidResult:
    """The post-state, read back after the commit."""

    invoice: Invoice
    already_void: bool
    released_parts: int = 0
    released_change_orders: int = 0
    released_time_entries: int = 0


def void_invoice(
    db: Session,
    invoice_id: UUID | str,
    *,
    actor: str,
    reason: str | None = None,
) -> InvoiceVoidResult:
    """Void ``invoice_id`` as ``actor`` and commit.

    ``actor`` is the acting user's id; a money mutation is never anonymous, so
    a blank one is refused before anything is read. ``reason`` lands in the
    ``invoice_voided`` audit row when given. An invoice that is already void
    is returned as-is with ``already_void=True`` and nothing written.

    ``invoice_id`` may be a UUID or its string form; one that does not parse
    is a 404, like a missing invoice.

    Raises ``InvoiceVoidRefused`` (404 missing/deleted/unparseable, 409 live
    payments or a locked accounting period). A refusal leaves nothing of the
    void in the session: a locked-period refusal rolls the session back first,
    so a caller that keeps its session cannot commit half a void. That
    rollback is the whole session's, so call this on a session with nothing
    else pending. (Not a savepoint: #661 was a `begin_nested()` around a
    ledger writer whose guard installer commits on first use.)
    """
    if not actor or not str(actor).strip():
        raise ValueError("void_invoice needs the acting user's id")
    # Lazy: routers.invoices imports this module.
    from gdx_dispatch.routers.invoices import _live_plan, _money, _to_float

    if not isinstance(invoice_id, UUID):
        try:
            invoice_id = UUID(str(invoice_id))
        except ValueError:
            raise InvoiceVoidRefused(404, "Invoice not found") from None
    invoice = db.execute(
        select(Invoice).where(Invoice.id == invoice_id, Invoice.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not invoice:
        raise InvoiceVoidRefused(404, "Invoice not found")
    if invoice.status == "void":
        return InvoiceVoidResult(invoice=invoice, already_void=True)  # idempotent

    has_payments = db.execute(
        select(func.count())
        .select_from(Payment)
        .where(Payment.invoice_id == invoice.id, Payment.voided_at.is_(None))
    ).scalar_one()
    if has_payments:
        raise InvoiceVoidRefused(
            409, "invoice has recorded payments — void or remove them first",
        )

    # GL S7: the P1 reversal alone would strand adjustment entries on AR.
    # GL S10: a pre-cutover-era invoice has no P1 to reverse — the void
    # posts its own entry clearing whatever AR it still holds (spec §5.7).
    # Ledger refusals surface as 409s with the reason, never bare 500s. The
    # transition is inside the try too: with posting on it reverses the P1
    # entry dated on the invoice date, so an invoice from a closed month is
    # refused right there (GDXA-1848 audit; the route answered 500 before).
    try:
        transition_invoice_status(db, invoice, "void", actor=actor)
        reverse_invoice_adjustments(db, invoice, actor=actor)
        settle_opening_on_void(db, invoice, actor=actor)
    except PeriodLockedError as exc:
        db.rollback()
        raise InvoiceVoidRefused(
            409, f"void posts into a locked accounting period — {exc}",
        ) from exc
    invoice.balance_due = _money(0)
    # A void is dead money — `core/billing_predicates.py` excludes voided
    # invoices from "is this job billed" for exactly that reason. But the parts
    # and change orders it claimed stayed stamped with its id, so they read
    # "billed" while no LIVE invoice bills them: gone from
    # `parts-needed?unbilled=true`, absent from Ready-for-Billing, and
    # unbillable forever. Money that was going to be charged silently stops
    # being charged.
    #
    # This is the same reasoning an auditor already applied to the soft-delete
    # path (see `delete_invoice`), and `void_untouched_autodraft` in
    # core/closeout_billing.py releases on its own void too. This was the one
    # void that did not — three void paths, two releases.
    released_parts = db.execute(
        update(JobPartNeeded)
        .where(JobPartNeeded.billed_invoice_id == invoice.id)
        .values(billed_invoice_id=None)
    ).rowcount
    from gdx_dispatch.routers.change_orders import ChangeOrder as _CO

    released_cos = db.execute(
        update(_CO)
        .where(_CO.billed_invoice_id == invoice.id)
        .values(billed_invoice_id=None)
    ).rowcount
    # The day rows it billed (migration 110) go back the same way, with their
    # own audit row staged into this same commit.
    from gdx_dispatch.core.closeout_billing import release_day_rows

    released_time_entries = release_day_rows(
        db, invoice, actor=actor, why="invoice_voided",
    )
    # M39 audit round 2: a void kills what is owed — an ACTIVE payment plan
    # scheduling money on a void invoice would be a standing lie. Cancel it
    # in the same transaction, with its own trail.
    _plan = _live_plan(db, invoice.id)
    if _plan is not None:
        _plan.status = "cancelled"
        _plan.cancelled_at = datetime.now(UTC)
        _plan.cancelled_by = actor
        log_audit_event_sync(
            db=db, tenant_id=None, user_id=actor,
            action="payment_plan_cancelled", entity_type="invoice",
            entity_id=str(invoice.id),
            details={"plan_id": str(_plan.id), "why": "invoice voided"},
        )
    # ONE commit, and the audit row is staged into it -- deliberately not the
    # commit-then-log-then-commit shape `delete_invoice` uses. Releasing a
    # claim puts billable work back on the checklist, so a release that lands
    # while its trail does not is a silent write, the defect class this repo
    # ranks highest. Staging both means the database decides them together:
    # either the void, the releases and the record of them are all durable, or
    # none of them are. (Adversarial review 2026-08-23 caught the two-commit
    # window; `delete_invoice` swallows the same failure into a log line.)
    # M12. A void is the ONE event after which the invoice can never owe
    # anything again, so every open intent on it is stale by definition. This
    # was the call site an adversarial review found missing: a voided invoice
    # whose customer still had the pay page open could be paid, and the webhook
    # would book that payment onto a void whose parts and change orders had
    # already gone back to the unbilled checklist.
    #
    # Enqueued AFTER the commit below, unlike its three siblings which each
    # already have a commit behind them at this point. The void deliberately
    # uses a SINGLE transaction — an earlier review forced the void, its
    # part/change-order releases and its audit row to land or roll back
    # together — so enqueueing here would let a `priority:high` worker cancel
    # the customer's live PaymentIntent at Stripe *before* the void is
    # durable. If the audit write or the commit then failed, the invoice would
    # still not be void and the intent would already be irreversibly cancelled:
    # the money side committed and the record rolled back, which is the exact
    # inversion of "the money commits first".
    details: dict[str, object] = {
        "total": _to_float(invoice.total),
        # The release counts belong in the trail: voiding an invoice puts work
        # back on the unbilled checklist, and "what changed" has to include
        # that, not just the invoice total.
        "released_parts": int(released_parts or 0),
        "released_change_orders": int(released_cos or 0),
        "released_time_entries": released_time_entries,
    }
    if reason:
        details["reason"] = reason
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=actor,
        action="invoice_voided",
        entity_type="invoice",
        entity_id=str(invoice.id),
        details=details,
    )
    db.commit()
    db.refresh(invoice)
    # Now that the void is durable. No `queued` flag in the audit row above:
    # it cannot be known before the commit, and inventing one there is what the
    # first version of this call site did. The sweep writes its own
    # `stale_payment_intents_canceled` row, so the trail exists either way.
    enqueue_stale_intent_sweep(invoice, why="invoice_voided", settled=True)
    return InvoiceVoidResult(
        invoice=invoice,
        already_void=False,
        released_parts=int(released_parts or 0),
        released_change_orders=int(released_cos or 0),
        released_time_entries=released_time_entries,
    )
