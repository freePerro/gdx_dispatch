"""
gdx_dispatch/integrations/native_webhooks.py — Native webhook endpoint management.

Allows tenants to register their own HTTP endpoints to receive real-time
event payloads signed with HMAC-SHA256.  Backed by the existing
WebhookEndpoint / WebhookDelivery models and the Celery delivery pipeline.

Endpoints:
  GET    /webhooks                         — list all endpoints
  POST   /webhooks                         — register a new endpoint
  DELETE /webhooks/{endpoint_id}           — deactivate an endpoint
  GET    /webhooks/{endpoint_id}/deliveries— delivery history
  POST   /webhooks/{endpoint_id}/retry     — re-queue a failed delivery
  POST   /webhooks/{endpoint_id}/test      — send a live test payload
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request as UReq
from urllib.request import urlopen
from uuid import UUID

from gdx_dispatch.core.ssrf_guard import OutboundURLBlocked, validate_outbound_url

log = logging.getLogger(__name__)

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.webhooks.delivery import deliver_webhook, sign_payload
from gdx_dispatch.core.webhooks.models import WebhookDelivery, WebhookEndpoint

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

SUPPORTED_EVENTS = [
    "job.created",
    "job.completed",
    "invoice.paid",
    "customer.created",
    "estimate.approved",
]


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class EndpointCreate(BaseModel):
    url: str
    events: list[str]
    secret: str | None = None


class RetryRequest(BaseModel):
    delivery_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post_url(url: str, payload: bytes, headers: dict[str, str]) -> int:
    try:
        validate_outbound_url(url)
    except OutboundURLBlocked:
        log.warning("native_webhooks._post_url blocked url=%s (SSRF guard)", url)
        return 0
    try:
        req = UReq(url, data=payload, headers=headers, method="POST")
        with urlopen(req, timeout=10) as resp:
            return int(resp.getcode())
    except HTTPError as exc:
        return int(exc.code)
    except Exception:
        log.warning("native_webhooks._post_url failed url=%s", url, exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# GET /webhooks — list all endpoints
# ---------------------------------------------------------------------------

@router.get("")
def list_endpoints(
    db: Session = Depends(get_db),
) -> JSONResponse:
    """List all registered webhook endpoints for this tenant."""
    try:
        rows = db.execute(
            select(WebhookEndpoint).order_by(desc(WebhookEndpoint.created_at))
        ).scalars().all()
        return JSONResponse(
            jsonable_encoder([
                {
                    "id": str(ep.id),
                    "url": ep.url,
                    "events": ep.events or [],
                    "is_active": ep.is_active,
                    "created_at": ep.created_at,
                }
                for ep in rows
            ])
        )
    except Exception as exc:
        return JSONResponse({"detail": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# POST /webhooks — register endpoint
# ---------------------------------------------------------------------------

@router.post("", status_code=201)
def create_endpoint(
    body: EndpointCreate,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Register a new webhook endpoint."""
    invalid = [e for e in body.events if e not in SUPPORTED_EVENTS]
    if invalid:
        return JSONResponse(
            {"detail": f"Unsupported events: {invalid}. Supported: {SUPPORTED_EVENTS}"},
            status_code=400,
        )
    if not body.url.startswith(("http://", "https://")):
        return JSONResponse({"detail": "url must start with http:// or https://"}, status_code=400)

    secret = body.secret or secrets.token_hex(32)
    try:
        ep = WebhookEndpoint(
            url=body.url,
            secret=secret,
            events=body.events,
            is_active=True,
        )
        db.add(ep)
        db.commit()
        db.refresh(ep)
        return JSONResponse(
            jsonable_encoder({
                "id": str(ep.id),
                "url": ep.url,
                "events": ep.events or [],
                "secret_hint": secret[-6:],
                "created_at": ep.created_at,
            }),
            status_code=201,
        )
    except Exception as exc:
        db.rollback()
        return JSONResponse({"detail": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# DELETE /webhooks/{endpoint_id} — deactivate endpoint
# ---------------------------------------------------------------------------

@router.delete("/{endpoint_id}")
def delete_endpoint(
    endpoint_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Deactivate (soft-delete) a webhook endpoint."""
    try:
        ep = db.get(WebhookEndpoint, UUID(endpoint_id))
        if not ep:
            return JSONResponse({"detail": "Endpoint not found"}, status_code=404)
        ep.is_active = False
        db.commit()
        return JSONResponse({"status": "deleted", "id": endpoint_id})
    except Exception as exc:
        db.rollback()
        return JSONResponse({"detail": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# GET /webhooks/{endpoint_id}/deliveries — delivery history
# ---------------------------------------------------------------------------

@router.get("/{endpoint_id}/deliveries")
def list_deliveries(
    endpoint_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Return the last 50 delivery attempts for an endpoint."""
    try:
        ep_uuid = UUID(endpoint_id)
        ep = db.get(WebhookEndpoint, ep_uuid)
        if not ep:
            return JSONResponse({"detail": "Endpoint not found"}, status_code=404)
        rows = db.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.endpoint_id == ep_uuid)
            .order_by(desc(WebhookDelivery.created_at))
            .limit(50)
        ).scalars().all()
        return JSONResponse(
            jsonable_encoder([
                {
                    "id": str(d.id),
                    "event_type": d.event_type,
                    "status": d.status,
                    "attempt_count": d.attempt_count,
                    "response_status": d.response_status,
                    "last_attempt_at": d.last_attempt_at,
                    "created_at": d.created_at,
                }
                for d in rows
            ])
        )
    except Exception as exc:
        return JSONResponse({"detail": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# POST /webhooks/{endpoint_id}/retry — re-queue a failed delivery
# ---------------------------------------------------------------------------

@router.post("/{endpoint_id}/retry")
def retry_delivery(
    endpoint_id: str,
    body: RetryRequest,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Re-queue a specific delivery for retry."""
    try:
        ep_uuid = UUID(endpoint_id)
        ep = db.get(WebhookEndpoint, ep_uuid)
        if not ep:
            return JSONResponse({"detail": "Endpoint not found"}, status_code=404)

        delivery = db.get(WebhookDelivery, UUID(body.delivery_id))
        if not delivery or delivery.endpoint_id != ep_uuid:
            return JSONResponse({"detail": "Delivery not found for this endpoint"}, status_code=404)

        # Reset to pending so the retry task will pick it up
        delivery.status = "pending"
        delivery.next_retry_at = None
        db.commit()

        # Dispatch Celery task if available, otherwise deliver inline
        try:
            from gdx_dispatch.core.webhooks.tasks import deliver_webhook_task
            deliver_webhook_task.delay(str(delivery.id))
        except Exception:
            asyncio.run(deliver_webhook(str(delivery.id), db))

        return JSONResponse({"status": "queued", "delivery_id": body.delivery_id})
    except Exception as exc:
        db.rollback()
        return JSONResponse({"detail": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# POST /webhooks/{endpoint_id}/test — send live test payload
# ---------------------------------------------------------------------------

@router.post("/{endpoint_id}/test")
def test_endpoint(
    endpoint_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Send a signed test payload to verify the endpoint is reachable."""
    try:
        ep = db.get(WebhookEndpoint, UUID(endpoint_id))
        if not ep:
            return JSONResponse({"detail": "Endpoint not found"}, status_code=404)
        if not ep.is_active:
            return JSONResponse({"detail": "Endpoint is inactive"}, status_code=400)

        payload_dict: dict[str, Any] = {
            "event": "test",
            "test": True,
            "data": {"message": "This is a test delivery from DispatchApp"},
        }
        payload_bytes = json.dumps(
            payload_dict, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "X-GDX-Event": "test",
            "X-GDX-Signature": sign_payload(payload_bytes, ep.secret),
            "X-GDX-Test": "1",
        }
        response_status = _post_url(ep.url, payload_bytes, headers)
        return JSONResponse({"status": "sent", "response_status": response_status})
    except Exception as exc:
        return JSONResponse({"detail": str(exc)}, status_code=500)
