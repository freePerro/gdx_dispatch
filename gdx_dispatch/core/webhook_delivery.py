"""
gdx_dispatch/core/webhook_delivery.py — public alias + high-level service layer for webhooks.

Provides a stable, flat import path (gdx_dispatch.core.webhook_delivery) so callers
do not need to know about the webhooks sub-package layout.  Low-level
implementation lives in gdx_dispatch/core/webhooks/delivery.py.

High-level service functions (deliver_webhook_event, get_delivery_log, etc.)
are implemented directly in this module.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import utcnow
from gdx_dispatch.core.webhooks.delivery import (  # noqa: F401 — re-exported public API
    RETRY_DELAYS,
    deliver_webhook,
    sign_payload,
)
from gdx_dispatch.core.webhooks.models import WebhookDelivery, WebhookEndpoint

try:
    from gdx_dispatch.core.webhooks.tasks import emit_webhook as _emit_webhook
except ImportError:
    logging.getLogger(__name__).exception("<module> caught exception")
    _emit_webhook = None  # type: ignore[assignment]

__all__ = [
    # Re-exported low-level API
    "deliver_webhook",
    "sign_payload",
    "RETRY_DELAYS",
    # High-level service functions
    "deliver_webhook_event",
    "get_delivery_log",
    "get_dead_letter_queue",
    "retry_delivery",
    "get_delivery_stats",
    "register_endpoint",
    "ping_endpoint",
]


# ---------------------------------------------------------------------------
# High-level service functions
# ---------------------------------------------------------------------------


def deliver_webhook_event(
    tenant_id: str,
    event_type: str,
    payload: dict[str, Any],
    db: Session,
) -> int:
    """Queue delivery of a webhook event to all subscribed active endpoints.

    If the Celery task layer (emit_webhook) is available it is used; otherwise
    delivery rows are created inline and a best-effort synchronous delivery is
    attempted.

    Returns the number of deliveries queued.
    """
    if _emit_webhook is not None:
        # Use Celery-backed emit for production path
        entity_id = str(payload.get("id", uuid4()))
        return _emit_webhook(event_type, entity_id, payload, tenant_id, db)

    # Fallback: inline — no Celery
    endpoints = (
        db.execute(
            select(WebhookEndpoint).where(WebhookEndpoint.is_active.is_(True))
        )
        .scalars()
        .all()
    )
    total = 0
    for ep in endpoints:
        if event_type not in (ep.events or []):
            continue
        entity_id = str(payload.get("id", uuid4()))
        idem_key = f"{tenant_id}:{event_type}:{entity_id}"[:100]
        row = WebhookDelivery(
            endpoint_id=ep.id,
            event_type=event_type,
            payload=payload,
            idempotency_key=idem_key,
            company_id=tenant_id,
        )
        db.add(row)
        total += 1
    if total:
        db.commit()
    return total


def get_delivery_log(
    tenant_id: str,
    db: Session,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return the most recent webhook delivery attempts as plain dicts.

    tenant_id is accepted for interface consistency; the DB session is already
    scoped to the tenant so no extra filtering is applied.
    """
    rows = (
        db.execute(
            select(WebhookDelivery)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(d.id),
            "endpoint_id": str(d.endpoint_id),
            "event_type": d.event_type,
            "status": d.status,
            "attempt_count": d.attempt_count,
            "last_attempt_at": d.last_attempt_at.isoformat() if d.last_attempt_at else None,
            "next_retry_at": d.next_retry_at.isoformat() if d.next_retry_at else None,
            "response_status": d.response_status,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in rows
    ]


def get_dead_letter_queue(
    tenant_id: str,
    db: Session,
) -> list[dict[str, Any]]:
    """Return all deliveries that exhausted retries (status='abandoned')."""
    rows = (
        db.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.status == "abandoned")
            .order_by(WebhookDelivery.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(d.id),
            "endpoint_id": str(d.endpoint_id),
            "event_type": d.event_type,
            "status": d.status,
            "attempt_count": d.attempt_count,
            "last_attempt_at": d.last_attempt_at.isoformat() if d.last_attempt_at else None,
            "next_retry_at": d.next_retry_at.isoformat() if d.next_retry_at else None,
            "response_status": d.response_status,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in rows
    ]


def retry_delivery(
    tenant_id: str,
    delivery_id: str,
    db: Session,
) -> dict[str, Any]:
    """Reset an abandoned delivery so it will be retried.

    Raises ValueError if delivery_id is not found or is not in 'abandoned'
    state.  Returns {"delivery_id": str, "queued": True} on success.
    """
    delivery = db.get(WebhookDelivery, UUID(delivery_id))
    if delivery is None:
        raise ValueError(f"Delivery not found: {delivery_id}")
    if delivery.status != "abandoned":
        raise ValueError(
            f"Delivery {delivery_id} is '{delivery.status}', not 'abandoned'"
        )
    delivery.status = "pending"
    delivery.attempt_count = 0
    delivery.next_retry_at = None
    db.commit()
    return {"delivery_id": delivery_id, "queued": True}


def get_delivery_stats(
    tenant_id: str,
    db: Session,
) -> dict[str, Any]:
    """Return aggregate delivery counts and success rate for this tenant."""
    row = db.execute(
        select(
            func.count(WebhookDelivery.id).label("total"),
            func.sum(
                case((WebhookDelivery.status == "delivered", 1), else_=0)
            ).label("delivered"),
            func.sum(
                case((WebhookDelivery.status == "failed", 1), else_=0)
            ).label("failed"),
            func.sum(
                case((WebhookDelivery.status == "abandoned", 1), else_=0)
            ).label("abandoned"),
            func.sum(
                case((WebhookDelivery.status == "pending", 1), else_=0)
            ).label("pending"),
        )
    ).one()

    total = row.total or 0
    delivered = row.delivered or 0
    failed = row.failed or 0
    abandoned = row.abandoned or 0
    pending = row.pending or 0

    return {
        "total": total,
        "delivered": delivered,
        "failed": failed,
        "abandoned": abandoned,
        "pending": pending,
        "success_rate": round(delivered / total, 4) if total > 0 else 0.0,
    }


def register_endpoint(
    tenant_id: str,
    url: str,
    events: list[str],
    secret: str,
    db: Session,
) -> dict[str, Any]:
    """Register a new webhook endpoint for the tenant.

    Returns {"endpoint_id": str, "url": str, "events": list, "created": True}.
    """
    ep = WebhookEndpoint(
        url=url,
        secret=secret,
        events=events,
        is_active=True,
    )
    db.add(ep)
    db.commit()
    db.refresh(ep)
    return {
        "endpoint_id": str(ep.id),
        "url": ep.url,
        "events": ep.events,
        "created": True,
    }


def ping_endpoint(
    tenant_id: str,
    endpoint_id: str,
    db: Session,
) -> dict[str, Any]:
    """Send a test payload to the given endpoint and return the HTTP result.

    Returns {"endpoint_id": str, "status_code": int | None, "success": bool}.
    Raises ValueError if the endpoint is not found.
    """

    ep = db.get(WebhookEndpoint, UUID(endpoint_id))
    if ep is None:
        raise ValueError(f"Endpoint not found: {endpoint_id}")

    test_payload: dict[str, Any] = {
        "event": "test",
        "message": "webhook test",
        "tenant_id": tenant_id,
        "endpoint_id": endpoint_id,
        "timestamp": utcnow().isoformat(),
    }
    payload_bytes = json.dumps(test_payload, separators=(",", ":"), default=str).encode()
    headers = {
        "X-GDX-Event": "test",
        "X-GDX-Delivery": str(uuid4()),
        "X-GDX-Signature": sign_payload(payload_bytes, ep.secret),
        "Content-Type": "application/json",
    }

    status_code: int | None = None
    try:
        req = Request(ep.url, data=payload_bytes, headers=headers, method="POST")
        with urlopen(req, timeout=10) as resp:
            status_code = int(resp.getcode())
    except HTTPError as exc:
        logging.getLogger(__name__).exception("ping_endpoint caught exception")
        status_code = int(exc.code)
    except Exception:
        logging.getLogger(__name__).exception("ping_endpoint caught exception")
        status_code = None

    return {
        "endpoint_id": endpoint_id,
        "status_code": status_code,
        "success": status_code is not None and 200 <= status_code < 300,
    }
