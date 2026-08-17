from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import timedelta
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event, utcnow
from gdx_dispatch.core.ssrf_guard import OutboundURLBlocked, validate_outbound_url
from gdx_dispatch.core.webhooks.models import AIAction, WebhookDelivery, WebhookEndpoint

RETRY_DELAYS = [5, 30, 120, 600, 1800, 7200, 21600, 86400]

log = logging.getLogger(__name__)


def sign_payload(payload_bytes: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


class _ValidatingRedirectHandler(HTTPRedirectHandler):
    """Re-run the SSRF guard on every redirect hop.

    ``urlopen`` follows 301/302/303/307/308 by default, and the stock handler
    does NOT re-validate the target — so a validated public URL that 307s to
    ``http://plugin-host:8000/internal/...`` or ``http://169.254.169.254/...``
    sails straight past the initial ``validate_outbound_url``. Validating each
    hop closes that hole (adversarial audit, security-F5).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        validate_outbound_url(newurl)  # raises OutboundURLBlocked → delivery fails closed
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = build_opener(_ValidatingRedirectHandler())


def _post(url: str, payload: bytes, headers: dict[str, str]) -> int:
    try:
        validate_outbound_url(url)
    except OutboundURLBlocked:
        log.warning("_post blocked url=%s (SSRF guard)", url)
        return 0
    try:
        with _opener.open(Request(url, data=payload, headers=headers, method="POST"), timeout=10) as resp:
            return int(resp.getcode())
    except HTTPError as exc:
        log.exception("_post caught HTTPError")
        return int(exc.code)
    except OutboundURLBlocked:
        # A redirect hop pointed at a disallowed host — treat as a failed (0),
        # non-2xx delivery so it retries/abandons like any other failure.
        log.warning("_post blocked redirect from url=%s (SSRF guard)", url)
        return 0


def _resolve_target(delivery: WebhookDelivery, db: Session):
    """Return (url, secret, active) for the delivery's receiver.

    A subscription-id (the live customer path, `webhook_subscriptions`) takes
    precedence; otherwise fall back to the legacy `webhook_endpoints` row so the
    existing public-API/register_endpoint path keeps working unchanged.
    """
    if delivery.subscription_id:
        # Imported lazily: the model lives in the router module; importing it at
        # top level would drag FastAPI into this worker-side module.
        from gdx_dispatch.routers.webhooks import WebhookSubscription

        sub = db.get(WebhookSubscription, UUID(str(delivery.subscription_id)))
        if not sub:
            return None
        active = bool(sub.active) and sub.deleted_at is None
        return (sub.url, sub.secret, active)
    if delivery.endpoint_id:
        ep = db.get(WebhookEndpoint, delivery.endpoint_id)
        if not ep:
            return None
        return (ep.url, ep.secret, bool(ep.is_active))
    return None


def _log_subscription_delivery(db: Session, delivery: WebhookDelivery, status: int | None, url: str) -> None:
    """Mirror an async delivery attempt into `webhook_delivery_logs` — the table
    the WebhooksView "Deliveries" tab actually reads. Without this, real events
    fire but the customer's history tab stays blank (QC caveat #1).

    Committed in its OWN transaction, called only AFTER the delivery row's state
    is already durable: a failed log write (bad constraint, oversize body) must
    never roll back the delivery's own commit — that would leave it 'pending'
    and the retry sweep would re-send an already-delivered webhook (audit-F(d)).
    """
    try:
        from gdx_dispatch.routers.webhooks import WebhookDeliveryLog

        db.add(
            WebhookDeliveryLog(
                company_id=delivery.company_id or "",
                subscription_id=UUID(str(delivery.subscription_id)),
                event=delivery.event_type,
                url=url,
                response_status=status,
                delivery_status="delivered" if status and 200 <= status < 300 else "failed",
                attempt=delivery.attempt_count,
                delivered_at=utcnow(),
            )
        )
        db.commit()
    except Exception:
        log.exception("webhook_delivery_log_write_failed id=%s", delivery.id)
        db.rollback()


async def deliver_webhook(delivery_id: str, db: Session) -> None:
    delivery = db.get(WebhookDelivery, UUID(delivery_id))
    if not delivery:
        return
    target = _resolve_target(delivery, db)
    if target is None:
        delivery.status = "failed"; db.commit(); return  # noqa: E701,E702
    url, secret, active = target
    if not active:
        delivery.status = "failed"; db.commit(); return  # noqa: E701,E702
    if not secret:
        # Signing material is required — a NULL secret would crash sign_payload
        # in the worker. Fail the delivery loudly rather than send unsigned.
        log.error("webhook_delivery_no_secret id=%s", delivery.id)
        delivery.status = "failed"; db.commit(); return  # noqa: E701,E702

    # Inject the delivery id into the envelope body (subscription deliveries
    # carry a dict envelope; legacy endpoint deliveries carry a bare payload).
    body_obj = dict(delivery.payload or {})
    if delivery.subscription_id:
        body_obj["delivery_id"] = str(delivery.id)
    payload = json.dumps(body_obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    headers = {
        "X-GDX-Event": delivery.event_type,
        "X-GDX-Delivery": str(delivery.id),
        "X-GDX-Signature": sign_payload(payload, secret),
        "X-Idempotency-Key": delivery.idempotency_key,
        "Content-Type": "application/json",
    }
    status = None
    try:
        status = await asyncio.to_thread(_post, url, payload, headers)
    except Exception:
        log.exception("deliver_webhook caught exception")
        status = None
    now = utcnow(); delivery.last_attempt_at = now; delivery.response_status = status  # noqa: E701,E702
    if status and 200 <= status < 300:
        delivery.status = "delivered"; delivery.next_retry_at = None  # noqa: E701,E702
    else:
        delivery.attempt_count += 1
        if delivery.attempt_count < len(RETRY_DELAYS):
            delivery.status = "pending"; delivery.next_retry_at = now + timedelta(seconds=RETRY_DELAYS[delivery.attempt_count - 1])  # noqa: E701,E702
        else:
            delivery.status = "abandoned"; delivery.next_retry_at = None  # noqa: E701,E702
            await log_audit_event(db, "webhook_abandoned", "system", "webhook_delivery", str(delivery.id), {"event_type": delivery.event_type, "endpoint_id": str(delivery.endpoint_id), "attempt_count": delivery.attempt_count, "response_status": status})
            db.add(
                AIAction(
                    action_type="webhook_dlq",
                    priority="high",
                    payload={
                        "endpoint_id": str(delivery.endpoint_id),
                        "subscription_id": str(delivery.subscription_id) if delivery.subscription_id else None,
                        "event_type": delivery.event_type,
                        "attempt_count": delivery.attempt_count,
                        "last_response_status": status,
                    },
                    status="pending",
                )
            )
    # Commit the delivery's own state FIRST, then mirror to the UI log table in a
    # separate transaction — so a log-row failure can never re-open the delivery.
    db.commit()
    if delivery.subscription_id:
        _log_subscription_delivery(db, delivery, status, url)
