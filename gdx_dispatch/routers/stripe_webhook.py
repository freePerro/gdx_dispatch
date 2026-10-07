from __future__ import annotations

import logging
import os

import stripe
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Payment, StripeWebhookEvent

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stripe"])


@router.post("/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Stripe event sink.

    Until 2026-08-04 this verified the signature, logged the event type and
    returned ok — it never dispatched, so ``handle_payment_webhook`` had no
    caller and the ONLY thing recording money was the browser calling
    ``/api/payments/confirm``. A customer who closed the tab after paying, or
    whose ACH settled days later (ACH is pending 1-2 business days and
    ``confirm`` is never called again), left a paid invoice looking unpaid.

    Now the signed webhook is authoritative and ``confirm`` is a
    best-effort fast path; ``_mark_invoice_paid`` is idempotent on the
    PaymentIntent id, so whichever arrives first records the payment and the
    other is a no-op.
    """
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        # Fail closed: without a verifier secret any caller could POST a fake
        # "payment succeeded" event and settle invoices for free.
        logger.error("stripe_webhook_secret_missing — refusing to process event")
        return JSONResponse(status_code=503, content={"detail": "webhook not configured"})
    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig, secret=secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        logger.exception("stripe_webhook_signature_invalid")
        return JSONResponse(status_code=400, content={"detail": "invalid signature"})

    event_type = event.get("type", "")
    event_id = str(event.get("id") or "")
    logger.info("stripe_webhook_received", extra={"event_type": event_type, "event_id": event_id})

    # GDXA-357. Stripe can deliver an event it already delivered, and the copy
    # can land after a later event for the same charge. Every handler is a
    # no-op on an immediate repeat, but not on that: a dispute withdrawal
    # redelivered after the dispute was won voided the payment again. This
    # stops a repeat of the SAME event id only; it does not order distinct
    # events. (A `succeeded` redelivered after a refund is refused in the
    # handler instead; see `_FINAL` below.)
    try:
        seen = bool(event_id) and db.get(StripeWebhookEvent, event_id) is not None
    except Exception:
        # Unreadable record (e.g. the table is gone after a downgrade): fall
        # back to the pre-GDXA-357 behaviour and let the handler run.
        db.rollback()
        logger.exception("stripe_webhook_event_lookup_failed event_id=%s", event_id)
        seen = False
    if seen:
        logger.info("stripe_webhook_duplicate event_id=%s type=%s — already handled", event_id, event_type)
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "result": {"status": "duplicate", "event_id": event_id}},
        )

    from gdx_dispatch.core.payments import handle_payment_webhook  # noqa: PLC0415

    try:
        result = handle_payment_webhook(dict(event), db)
    except Exception:
        # Return 500 so Stripe retries with backoff rather than dropping a
        # money event on the floor. No event row is written, so the retry runs.
        logger.exception("stripe_webhook_dispatch_failed event_id=%s", event_id)
        return JSONResponse(status_code=500, content={"detail": "handler error"})

    _record_handled(db, event_id, event_type, result, dict(event))
    return JSONResponse(status_code=200, content={"status": "ok", "result": result})


# Only these outcomes are recorded as handled: the handler acted, and a re-run
# of the same event can only repeat or undo that. Every other outcome stays
# unrecorded, so a redelivery (or an operator's Dashboard re-send after fixing
# the data) runs the handler again, exactly as before GDXA-357. That matters
# for `reinstate_needs_review` (re-sent once a person has fixed the void) and
# `no_payment_to_reinstate` (a reinstatement that arrived before its void).
#
# `paid` is deliberately NOT here. M14 recovers a payment a stale failure event
# wrongly voided by re-sending the same `succeeded`; recording it would answer
# that re-send with `duplicate` and leave the collected money off the books.
# The success's own replay hazard (re-booking after a refund) is closed in the
# handler instead, by `core.payments._deliberately_voided`.
_FINAL = frozenset({
    "reversed", "reinstated",
    "dispute_inquiry_noted", "ach_processing_noted", "ach_verification_noted",
})
# One outcome is final only when the database says so. `no_payment_to_reverse`
# is "already done" for the `funds_withdrawn` Stripe sends after
# `charge.dispute.created` voided the payment: left unrecorded, its redelivery
# after the dispute is won voids the payment again. Anywhere else it is "not
# yet": a reversal that arrived before its payment was recorded, or one that
# found the payment voided by a DIFFERENT kind of reversal (a refund landing on
# a payment a stale failure voided, which M14's re-send can bring back; /audit
# probe 2026-10-07). So it is final only when a void of the event's own kind
# is already on the row.
_FINAL_IF_ITS_KIND_VOIDED = "no_payment_to_reverse"


def _voided_by_its_own_kind(db: Session, event: dict) -> bool:
    """A Payment row for this event's PaymentIntent was voided by a reversal of
    the same kind as this event (dispute by dispute, refund by refund)."""
    from gdx_dispatch.core.payments import _DISPUTE_VOID_REASONS, _FAILURE_VOID_REASONS  # noqa: PLC0415

    event_type = str(event.get("type") or "")
    kinds = {
        "charge.refunded": frozenset({"charge.refunded"}),
        "charge.dispute.created": _DISPUTE_VOID_REASONS,
        "charge.dispute.funds_withdrawn": _DISPUTE_VOID_REASONS,
        "charge.failed": _FAILURE_VOID_REASONS,
        "payment_intent.payment_failed": _FAILURE_VOID_REASONS,
    }.get(event_type)
    obj = (event.get("data") or {}).get("object") or {}
    # `charge.*` events name the intent in `payment_intent`; for
    # `payment_intent.*` the object IS the intent.
    reference = str(obj.get("payment_intent") or "") or (
        str(obj.get("id") or "") if event_type.startswith("payment_intent.") else ""
    )
    if not kinds or not reference:
        return False
    reasons = db.scalars(
        select(Payment.voided_reason).where(Payment.reference == reference, Payment.voided_at.is_not(None))
    ).all()
    return any(reason in kinds for reason in reasons)


def _record_handled(db: Session, event_id: str, event_type: str, result: dict, event: dict) -> None:
    """Mark ``event_id`` handled. Never raises: the handler's work is committed.

    Written after the handler returns rather than inside its transaction,
    because the handlers commit as they go. The cost of that split is a crash
    between the two commits: the event is then handled again on redelivery,
    which is the immediate-repeat case every handler is already a no-op for
    (`tests/test_stripe_webhook_redelivery.py`).
    """
    if not event_id:
        return
    status = str((result or {}).get("status") or "")
    if status == "failed":  # payment_intent.payment_failed nests its reversal
        status = str((result or {}).get("reversal") or status)
    if status not in _FINAL and status != _FINAL_IF_ITS_KIND_VOIDED:
        return
    try:
        if status == _FINAL_IF_ITS_KIND_VOIDED and not _voided_by_its_own_kind(db, event):
            return
    except Exception:
        # Unreadable: leave it unrecorded. The cost is a later redelivery
        # running the handler again, which is the pre-GDXA-357 behaviour.
        db.rollback()
        logger.exception("stripe_webhook_event_record_check_failed event_id=%s", event_id)
        return
    try:
        db.add(StripeWebhookEvent(event_id=event_id, event_type=event_type[:100], result_status=status[:64] or None))
        db.commit()
    except IntegrityError:
        # A concurrent delivery of the same event got there first; it is
        # recorded either way.
        db.rollback()
    except Exception:
        db.rollback()
        logger.exception(
            "stripe_webhook_event_record_failed event_id=%s type=%s — handled, but a "
            "late redelivery of it will be handled again",
            event_id, event_type,
        )
