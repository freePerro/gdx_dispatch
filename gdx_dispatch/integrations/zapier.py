"""
gdx_dispatch/integrations/zapier.py — Zapier REST Hooks integration.

Implements the Zapier REST Hooks pattern so tenants can connect
DispatchApp events to any Zapier zap without custom polling.

Supported trigger events:
  job.created, job.completed, invoice.paid, customer.created, estimate.approved

REST Hooks flow:
  1. Zapier POSTs to /subscribe with {hook_url, event_type} — we store a WebhookEndpoint.
  2. Zapier DELETEs to /unsubscribe — we deactivate the WebhookEndpoint.
  3. Zapier GETs /list for reverse polling (optional, Zapier uses this for testing zap setup).
  4. Zapier calls /test to verify the hook_url is reachable before enabling the zap.

Actual delivery of events uses emit_webhook() from gdx_dispatch.core.webhooks.tasks,
which creates a WebhookDelivery row and dispatches a Celery task.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request as UReq
from urllib.request import urlopen

from gdx_dispatch.core.ssrf_guard import OutboundURLBlocked, validate_outbound_url

log = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.webhooks.models import WebhookEndpoint

SUPPORTED_EVENTS = [
    "job.created",
    "job.completed",
    "invoice.paid",
    "customer.created",
    "estimate.approved",
]

router = APIRouter(prefix="/integrations/zapier", tags=["zapier"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class ZapierSubscribe(BaseModel):
    hook_url: str
    event_type: str


class ZapierUnsubscribe(BaseModel):
    hook_url: str


class ZapierTest(BaseModel):
    hook_url: str
    event_type: str


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _sign(payload_bytes: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


def _post_url(url: str, payload: bytes, headers: dict[str, str]) -> int:
    try:
        validate_outbound_url(url)
    except OutboundURLBlocked:
        log.warning("zapier._post_url blocked url=%s (SSRF guard)", url)
        return 0
    try:
        req = UReq(url, data=payload, headers=headers, method="POST")
        with urlopen(req, timeout=10) as resp:
            return int(resp.getcode())
    except HTTPError as exc:
        return int(exc.code)
    except Exception:
        log.warning("zapier._post_url failed url=%s", url, exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/subscribe", status_code=201)
def zapier_subscribe(
    body: ZapierSubscribe,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Register a Zapier webhook subscription (REST Hook subscribe)."""
    if body.event_type not in SUPPORTED_EVENTS:
        return JSONResponse(
            {"detail": f"Unsupported event_type. Supported: {SUPPORTED_EVENTS}"},
            status_code=400,
        )
    try:
        # Dedup: if an active subscription for this url+event already exists, return it
        existing = db.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.url == body.hook_url,
                WebhookEndpoint.is_active.is_(True),
            )
        ).scalars().first()
        if existing and body.event_type in (existing.events or []):
            return JSONResponse(
                jsonable_encoder({
                    "id": str(existing.id),
                    "hook_url": existing.url,
                    "event_type": body.event_type,
                    "created_at": existing.created_at,
                }),
                status_code=200,
            )

        ep = WebhookEndpoint(
            url=body.hook_url,
            secret=secrets.token_hex(32),
            events=[body.event_type],
            is_active=True,
        )
        db.add(ep)
        db.commit()
        db.refresh(ep)
        return JSONResponse(
            jsonable_encoder({
                "id": str(ep.id),
                "hook_url": ep.url,
                "event_type": body.event_type,
                "created_at": ep.created_at,
            }),
            status_code=201,
        )
    except Exception as exc:
        db.rollback()
        return JSONResponse({"detail": str(exc)}, status_code=500)


@router.delete("/unsubscribe")
def zapier_unsubscribe(
    body: ZapierUnsubscribe,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Deregister a Zapier webhook subscription (REST Hook unsubscribe)."""
    try:
        ep = db.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.url == body.hook_url,
                WebhookEndpoint.is_active.is_(True),
            )
        ).scalars().first()
        if not ep:
            return JSONResponse({"detail": "Subscription not found"}, status_code=404)
        ep.is_active = False
        db.commit()
        return JSONResponse({"status": "unsubscribed", "id": str(ep.id)})
    except Exception as exc:
        db.rollback()
        return JSONResponse({"detail": str(exc)}, status_code=500)


@router.get("/list")
def zapier_list(
    db: Session = Depends(get_db),
) -> JSONResponse:
    """List active Zapier subscriptions (used by Zapier for reverse polling)."""
    try:
        rows = db.execute(
            select(WebhookEndpoint).where(WebhookEndpoint.is_active.is_(True))
        ).scalars().all()
        return JSONResponse(
            jsonable_encoder([
                {
                    "id": str(ep.id),
                    "url": ep.url,
                    "events": ep.events or [],
                    "created_at": ep.created_at,
                }
                for ep in rows
            ])
        )
    except Exception as exc:
        return JSONResponse({"detail": str(exc)}, status_code=500)


@router.post("/test")
def zapier_test(
    body: ZapierTest,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Send a test event to a hook URL so Zapier can verify connectivity."""
    if body.event_type not in SUPPORTED_EVENTS:
        return JSONResponse(
            {"detail": f"Unsupported event_type. Supported: {SUPPORTED_EVENTS}"},
            status_code=400,
        )

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", "test"))
    test_secret = secrets.token_hex(16)

    sample_payloads: dict[str, Any] = {
        "job.created": {"id": "job-sample-001", "title": "Spring Tune-Up", "status": "Scheduled", "tenant_id": tenant_id},
        "job.completed": {"id": "job-sample-002", "title": "Panel Replacement", "status": "Complete", "tenant_id": tenant_id},
        "invoice.paid": {"id": "inv-sample-001", "amount": 250.00, "customer": "Jane Smith", "tenant_id": tenant_id},
        "customer.created": {"id": "cust-sample-001", "name": "Jane Smith", "email": "jane@example.com", "tenant_id": tenant_id},
        "estimate.approved": {"id": "est-sample-001", "total": 1200.00, "customer": "Jane Smith", "tenant_id": tenant_id},
    }

    payload_dict = {
        "event": body.event_type,
        "test": True,
        "data": sample_payloads.get(body.event_type, {}),
    }
    payload_bytes = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"), default=str).encode()
    headers = {
        "Content-Type": "application/json",
        "X-GDX-Event": body.event_type,
        "X-GDX-Signature": _sign(payload_bytes, test_secret),
        "X-GDX-Test": "1",
    }

    response_status = _post_url(body.hook_url, payload_bytes, headers)
    return JSONResponse({"status": "sent", "response_status": response_status})
