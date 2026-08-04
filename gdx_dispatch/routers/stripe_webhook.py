from __future__ import annotations

import logging
import os

import stripe
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db

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
    logger.info("stripe_webhook_received", extra={"event_type": event_type, "event_id": event.get("id")})

    from gdx_dispatch.core.payments import handle_payment_webhook  # noqa: PLC0415

    try:
        result = handle_payment_webhook(dict(event), db)
    except Exception:
        # Return 500 so Stripe retries with backoff rather than dropping a
        # money event on the floor.
        logger.exception("stripe_webhook_dispatch_failed event_id=%s", event.get("id"))
        return JSONResponse(status_code=500, content={"detail": "handler error"})

    return JSONResponse(status_code=200, content={"status": "ok", "result": result})
