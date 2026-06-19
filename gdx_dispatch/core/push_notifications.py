"""Web Push notification support for GDX tenants.

Uses pywebpush for VAPID-signed push delivery.
In-memory subscription store (per-process) — suitable for single-worker
deployments; replace with DB-backed storage for multi-worker setups.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import Request, APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ── Optional pywebpush import ──────────────────────────────────────────────

try:
    from pywebpush import WebPushException, webpush  # type: ignore

    _WEBPUSH_AVAILABLE = True
except ImportError:
    _WEBPUSH_AVAILABLE = False
    webpush = None  # type: ignore
    WebPushException = Exception  # type: ignore
    logger.warning("pywebpush not installed — push notifications disabled")

# ── In-memory subscription store ──────────────────────────────────────────
# Keyed first by tenant_id (tenant isolation), then by endpoint URL.
# Never iterate the outer dict in send paths — always scope to one tenant.
_subscriptions: dict[str, dict[str, dict[str, Any]]] = {}


def _tenant_from_request(request: Any) -> str:
    tenant = getattr(request.state, "tenant", None) or {}
    tid = str(tenant.get("id") or "")
    if not tid:
        raise HTTPException(status_code=400, detail="tenant context required for push")
    return tid

router = APIRouter(prefix="/api/push", tags=["push"])

# ── Pydantic schemas ───────────────────────────────────────────────────────


class PushSubscriptionCreate(BaseModel):
    endpoint: str
    p256dh: str
    auth: str


class PushUnsubscribeRequest(BaseModel):
    endpoint: str


class PushNotificationPayload(BaseModel):
    title: str
    body: str
    url: str = "/dashboard"
    icon: str = "/static/icon-192.png"


# ── Helper ─────────────────────────────────────────────────────────────────


def send_push_notification(subscription_info: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Send a Web Push notification to a single subscription.

    Returns True on success, False on failure.
    """
    if not _WEBPUSH_AVAILABLE or webpush is None:
        logger.warning("pywebpush unavailable — skipping push notification")
        return False

    private_key = os.environ.get("VAPID_PRIVATE_KEY", "")
    public_key = os.environ.get("VAPID_PUBLIC_KEY", "")

    if not private_key or not public_key:
        logger.warning("VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY not set — skipping push")
        return False

    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=private_key,
            vapid_claims={
                "sub": "mailto:admin@example.com",
            },
        )
        return True
    except WebPushException as exc:  # failure is handled by returning False per docstring
        logger.warning("WebPush failed for endpoint %s: %s", subscription_info.get("endpoint", "?"), exc)
        return False
    except Exception as exc:  # noqa: BLE001  # failure is handled by returning False per docstring
        logger.warning("Unexpected push error: %s", exc)
        return False


def _build_subscription_info(sub: dict[str, Any]) -> dict[str, Any]:
    """Convert stored subscription dict to pywebpush-compatible format."""
    return {
        "endpoint": sub["endpoint"],
        "keys": {
            "p256dh": sub["p256dh"],
            "auth": sub["auth"],
        },
    }


# ── Routes ─────────────────────────────────────────────────────────────────


@router.post("/subscribe", status_code=status.HTTP_201_CREATED)
async def subscribe(body: PushSubscriptionCreate, request: Request) -> JSONResponse:
    """Save a push subscription for the current browser/device (tenant-scoped)."""
    tid = _tenant_from_request(request)
    _subscriptions.setdefault(tid, {})[body.endpoint] = {
        "endpoint": body.endpoint,
        "p256dh": body.p256dh,
        "auth": body.auth,
    }
    logger.info("Push subscription saved tenant=%s (tenant_total=%d)", tid, len(_subscriptions[tid]))
    return JSONResponse(content={"status": "subscribed", "endpoint": body.endpoint})


@router.delete("/unsubscribe")
async def unsubscribe(body: PushUnsubscribeRequest, request: Request) -> JSONResponse:
    """Remove a push subscription (scoped to caller's tenant)."""
    tid = _tenant_from_request(request)
    tenant_map = _subscriptions.get(tid, {})
    removed = tenant_map.pop(body.endpoint, None)
    if removed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    logger.info("Push subscription removed tenant=%s (tenant_total=%d)", tid, len(tenant_map))
    return JSONResponse(content={"status": "unsubscribed"})


@router.post("/send")
async def send_to_all(payload: PushNotificationPayload, request: Request) -> JSONResponse:
    """Send a push notification to all subscriptions FOR THE CALLER'S TENANT.

    Historically named `send_to_all` — misleading; now strictly tenant-scoped.
    """
    tid = _tenant_from_request(request)
    tenant_subs = _subscriptions.get(tid, {})
    if not tenant_subs:
        return JSONResponse(content={"status": "no_subscribers", "sent": 0, "failed": 0})

    push_data = {
        "title": payload.title,
        "body": payload.body,
        "url": payload.url,
        "icon": payload.icon,
    }

    sent = 0
    failed = 0
    stale_endpoints: list[str] = []

    for endpoint, sub in list(tenant_subs.items()):
        sub_info = _build_subscription_info(sub)
        ok = send_push_notification(sub_info, push_data)
        if ok:
            sent += 1
        else:
            failed += 1
            stale_endpoints.append(endpoint)

    if _WEBPUSH_AVAILABLE and os.environ.get("VAPID_PRIVATE_KEY"):
        for ep in stale_endpoints:
            tenant_subs.pop(ep, None)

    return JSONResponse(content={"status": "sent", "sent": sent, "failed": failed})


@router.post("/send-user/{user_id}")
async def send_to_user(user_id: str, payload: PushNotificationPayload) -> JSONResponse:
    """Send a push notification to a specific user (stub — user lookup TBD)."""
    return JSONResponse(content={"status": "queued", "user_id": user_id})
