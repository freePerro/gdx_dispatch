from __future__ import annotations

import logging
import os

import stripe
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stripe"])


@router.post("/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request) -> JSONResponse:
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig, secret=secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        logger.exception("stripe_webhook_signature_invalid")
        return JSONResponse(status_code=400, content={"detail": "invalid signature"})

    event_type = event.get("type", "")
    logger.info("stripe_webhook_received", extra={"event_type": event_type, "event_id": event.get("id")})
    return JSONResponse(status_code=200, content={"status": "ok"})
