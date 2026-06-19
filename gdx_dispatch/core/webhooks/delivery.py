from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID

from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event, utcnow
from gdx_dispatch.core.ssrf_guard import OutboundURLBlocked, validate_outbound_url
from gdx_dispatch.core.webhooks.models import AIAction, WebhookDelivery, WebhookEndpoint

RETRY_DELAYS = [5, 30, 120, 600, 1800, 7200, 21600, 86400]


def sign_payload(payload_bytes: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


def _post(url: str, payload: bytes, headers: dict[str, str]) -> int:
    try:
        validate_outbound_url(url)
    except OutboundURLBlocked:
        logging.getLogger(__name__).warning("_post blocked url=%s (SSRF guard)", url)
        return 0
    try:
        with urlopen(Request(url, data=payload, headers=headers, method="POST"), timeout=10) as resp:
            return int(resp.getcode())
    except HTTPError as exc:
        logging.getLogger(__name__).exception("_post caught exception")
        return int(exc.code)


async def deliver_webhook(delivery_id: str, db: Session) -> None:
    delivery = db.get(WebhookDelivery, UUID(delivery_id))
    if not delivery:
        return
    endpoint = db.get(WebhookEndpoint, delivery.endpoint_id)
    if not endpoint or not endpoint.is_active:
        delivery.status = "failed"; db.commit(); return  # noqa: E701,E702
    payload = json.dumps(delivery.payload or {}, sort_keys=True, separators=(",", ":"), default=str).encode()
    headers = {
        "X-GDX-Event": delivery.event_type,
        "X-GDX-Delivery": str(delivery.id),
        "X-GDX-Signature": sign_payload(payload, endpoint.secret),
        "X-Idempotency-Key": delivery.idempotency_key,
        "Content-Type": "application/json",
    }
    status = None
    try:
        status = await asyncio.to_thread(_post, endpoint.url, payload, headers)
    except Exception:
        logging.getLogger(__name__).exception("deliver_webhook caught exception")
        status = None
    now = utcnow(); delivery.last_attempt_at = now; delivery.response_status = status  # noqa: E701,E702
    if status and 200 <= status < 300:
        delivery.status = "delivered"; delivery.next_retry_at = None; db.commit(); return  # noqa: E701,E702
    delivery.attempt_count += 1
    if delivery.attempt_count < len(RETRY_DELAYS):
        delivery.status = "pending"; delivery.next_retry_at = now + timedelta(seconds=RETRY_DELAYS[delivery.attempt_count - 1])  # noqa: E701,E702
    else:
        delivery.status = "abandoned"; delivery.next_retry_at = None  # noqa: E701,E702
        await log_audit_event(db, "webhook_abandoned", "system", "webhook_delivery", str(delivery.id), {"event_type": delivery.event_type, "endpoint_id": str(delivery.endpoint_id), "attempt_count": delivery.attempt_count, "response_status": status})
        dlq = AIAction(
            action_type="webhook_dlq",
            priority="high",
            payload={
                "endpoint_id": str(delivery.endpoint_id),
                "event_type": delivery.event_type,
                "attempt_count": delivery.attempt_count,
                "last_response_status": status,
            },
            status="pending",
        )
        db.add(dlq)
    db.commit()
