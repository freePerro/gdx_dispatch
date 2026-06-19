"""Phase 1.5 E3 + E4 — DB-backed Web Push subscriptions + send helper.

Replaces the in-memory ``_subscriptions`` dict in
``gdx_dispatch/core/push_notifications.py`` (which lost subs on every worker
restart and couldn't fan out across workers). Tenant scope = the
SQLAlchemy connection; no tenant_id column on the subscription row.

Public surface:
    upsert_subscription(db, *, user_id, endpoint, p256dh, auth, ...)
    revoke_subscription(db, *, endpoint)
    list_subscriptions_for_user(db, user_id)
    send_push(db, *, user_id, title, body, url=..., data=...) -> SendResult

The legacy in-memory routes in push_notifications.py stay for now —
the new flow lives at gdx_dispatch/routers/push.py and is registered alongside
them so the migration can be staged.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import PushSubscription

log = logging.getLogger(__name__)

try:
    from pywebpush import WebPushException, webpush  # type: ignore

    _WEBPUSH_AVAILABLE = True
except ImportError:
    _WEBPUSH_AVAILABLE = False
    webpush = None  # type: ignore
    WebPushException = Exception  # type: ignore


@dataclass
class SendResult:
    sent: int = 0
    failed: int = 0
    skipped_no_subs: bool = False
    skipped_no_vapid: bool = False
    skipped_no_pywebpush: bool = False
    pruned_endpoints: list[str] = field(default_factory=list)


def _vapid_keys() -> tuple[str, str]:
    """Return (private_key_pem, public_key_b64).

    Two env shapes accepted because docker-compose ``--env-file`` can't
    carry a multi-line PEM:

    * ``VAPID_PRIVATE_KEY``        — raw PEM (works in shell exports /
                                     k8s secrets / one-line PEMs).
    * ``VAPID_PRIVATE_KEY_B64``    — base64-encoded PEM (single line,
                                     compose-friendly). Decoded here.

    The public key is always urlsafe-b64 (single line by construction).
    """
    public = os.environ.get("VAPID_PUBLIC_KEY", "")
    private_pem = os.environ.get("VAPID_PRIVATE_KEY", "")
    if not private_pem:
        b64 = os.environ.get("VAPID_PRIVATE_KEY_B64", "")
        if b64:
            import base64 as _b64
            try:
                private_pem = _b64.b64decode(b64).decode("ascii")
            except Exception:  # noqa: BLE001
                log.warning("VAPID_PRIVATE_KEY_B64 set but failed to decode")
                private_pem = ""
    return private_pem, public


def upsert_subscription(
    db: Session,
    *,
    user_id: str,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None = None,
) -> PushSubscription:
    """Create-or-update the subscription row keyed by (endpoint).

    A given browser/device gets a unique endpoint URL from its push
    service, so endpoint is the natural unique key. If the same user
    re-subscribes after a permission flip we just refresh the keys and
    bump ``last_seen_at`` instead of inserting a duplicate.
    """
    now = datetime.now(timezone.utc)
    existing = db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    ).scalar_one_or_none()
    if existing is None:
        row = PushSubscription(
            id=str(uuid4()),
            user_id=user_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=user_agent,
            subscribed_at=now,
            last_seen_at=now,
        )
        db.add(row)
        db.flush()
        return row
    existing.user_id = user_id  # support user re-binding the same browser
    existing.p256dh = p256dh
    existing.auth = auth
    if user_agent is not None:
        existing.user_agent = user_agent
    existing.last_seen_at = now
    existing.revoked_at = None
    db.flush()
    return existing


def revoke_subscription(db: Session, *, endpoint: str) -> bool:
    row = db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    ).scalar_one_or_none()
    if row is None:
        return False
    row.revoked_at = datetime.now(timezone.utc)
    db.flush()
    return True


def list_subscriptions_for_user(db: Session, user_id: str) -> list[PushSubscription]:
    return list(
        db.execute(
            select(PushSubscription).where(
                PushSubscription.user_id == user_id,
                PushSubscription.revoked_at.is_(None),
            )
        ).scalars().all()
    )


def send_push(
    db: Session,
    *,
    user_id: str,
    title: str,
    body: str,
    url: str = "/dashboard",
    icon: str = "/static/icon-192.png",
    data: dict[str, Any] | None = None,
) -> SendResult:
    """Send a Web Push notification to every active subscription for this user.

    Failure modes:
      * ``pywebpush`` not installed → ``skipped_no_pywebpush=True``;
        caller should fall back to in-app or email per the tenant's
        ``push_fallback_mode`` setting.
      * VAPID keys not configured → ``skipped_no_vapid=True``.
      * No subscriptions for this user → ``skipped_no_subs=True``.
      * Per-endpoint failure (404 / 410) → counted in ``failed`` and the
        endpoint added to ``pruned_endpoints``; the row is revoked so
        future sends don't re-hit dead endpoints.

    Caller commits.
    """
    result = SendResult()
    if not _WEBPUSH_AVAILABLE:
        result.skipped_no_pywebpush = True
        return result
    private_key, public_key = _vapid_keys()
    if not private_key or not public_key:
        result.skipped_no_vapid = True
        return result

    subs = list_subscriptions_for_user(db, user_id)
    if not subs:
        result.skipped_no_subs = True
        return result

    payload = {"title": title, "body": body, "url": url, "icon": icon}
    if data:
        payload["data"] = data
    payload_json = json.dumps(payload)

    vapid_claims = {"sub": "mailto:admin@example.com"}
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=payload_json,
                vapid_private_key=private_key,
                vapid_claims=dict(vapid_claims),
            )
            sub.last_seen_at = datetime.now(timezone.utc)
            result.sent += 1
        except WebPushException as exc:  # noqa: PERF203
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in (404, 410):
                # Endpoint is gone — revoke so we stop trying.
                sub.revoked_at = datetime.now(timezone.utc)
                result.pruned_endpoints.append(sub.endpoint)
            log.warning(
                "webpush_failed user=%s endpoint=%s status=%s err=%s",
                user_id, sub.endpoint, status_code, exc,
            )
            result.failed += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("webpush_unexpected user=%s err=%s", user_id, exc)
            result.failed += 1
    db.flush()
    return result
