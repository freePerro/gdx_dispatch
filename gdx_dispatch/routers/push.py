"""Phase 1.5 E2 — DB-backed push subscription router.

Distinct from the legacy in-memory routes at /api/push (those stay for
backwards-compat during the transition). The new flow lives at
/api/push/v2.

Endpoints:
    POST   /api/push/v2/subscribe       — upsert (user_id derived from JWT)
    DELETE /api/push/v2/unsubscribe     — revoke
    GET    /api/push/v2/me              — list current user's subs
    GET    /api/push/v2/vapid-public    — return public VAPID key for the SW
    GET    /api/push/v2/fallback-mode   — tenant's push_fallback_mode setting
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.push_subscriptions import (
    list_subscriptions_for_user,
    revoke_subscription,
    upsert_subscription,
)
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/push/v2", tags=["push-v2"])


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "")


class SubscribeBody(BaseModel):
    endpoint: str = Field(min_length=10, max_length=2000)
    p256dh: str = Field(min_length=1, max_length=200)
    auth: str = Field(min_length=1, max_length=200)
    user_agent: str | None = Field(default=None, max_length=500)


class UnsubscribeBody(BaseModel):
    endpoint: str = Field(min_length=10, max_length=2000)


@router.post("/subscribe", status_code=201)
def subscribe(
    request: Request,
    body: SubscribeBody,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    uid = _uid(user)
    if not uid:
        raise HTTPException(status_code=401, detail="login required")
    row = upsert_subscription(
        db,
        user_id=uid,
        endpoint=body.endpoint,
        p256dh=body.p256dh,
        auth=body.auth,
        user_agent=body.user_agent,
    )
    db.commit()
    log_audit_event_sync(
        db, tenant_id=_tid(request), user_id=uid,
        action="push_subscribe", entity_type="push_subscription",
        entity_id=row.id, details={"endpoint": body.endpoint[:80]},
        request=request,
    )
    return {"status": "subscribed", "id": row.id}


@router.delete("/unsubscribe")
def unsubscribe(
    request: Request,
    body: UnsubscribeBody,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    uid = _uid(user)
    if not uid:
        raise HTTPException(status_code=401, detail="login required")
    ok = revoke_subscription(db, endpoint=body.endpoint)
    db.commit()
    if not ok:
        raise HTTPException(status_code=404, detail="subscription not found")
    log_audit_event_sync(
        db, tenant_id=_tid(request), user_id=uid,
        action="push_unsubscribe", entity_type="push_subscription",
        entity_id="", details={"endpoint": body.endpoint[:80]},
        request=request,
    )
    return {"status": "unsubscribed"}


@router.get("/me")
def list_mine(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    uid = _uid(user)
    if not uid:
        raise HTTPException(status_code=401, detail="login required")
    rows = list_subscriptions_for_user(db, uid)
    return [
        {
            "id": r.id,
            "endpoint": r.endpoint,
            "user_agent": r.user_agent,
            "subscribed_at": r.subscribed_at.isoformat() if r.subscribed_at else None,
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
        }
        for r in rows
    ]


@router.get("/vapid-public")
def vapid_public(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    """Return the public VAPID key for the service worker to subscribe with.

    Returns an empty string when push isn't configured — frontend should
    interpret that as "push unavailable; show no Enable-notifications CTA."
    """
    return {"public_key": os.environ.get("VAPID_PUBLIC_KEY", "")}


@router.get("/fallback-mode")
def fallback_mode(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Phase 1.5 E5 — tenant's push_fallback_mode setting."""
    from gdx_dispatch.core.tenant_mobile_settings import get_tenant_mobile_setting

    mode = get_tenant_mobile_setting(
        db, "tech_mobile.push_fallback_mode", default="badge_only", request=request
    )
    return {"mode": mode}
