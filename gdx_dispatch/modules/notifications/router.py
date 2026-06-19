from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.modules.notifications.models import DeviceToken, NotificationLog, NotificationPreference
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(prefix="/api", tags=["notifications"])


# ── Pydantic schemas ───────────────────────────────────────────────────────────


class PreferencePatch(BaseModel):
    notification_type: str
    channel: str
    is_enabled: bool


class DeviceRegister(BaseModel):
    platform: str
    token: str


# ── Notification log endpoints ─────────────────────────────────────────────────


@router.get("/notifications", response_model=None)
def list_notifications(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[NotificationLog]:
    """Return all unread in-app notifications for the current user."""
    q = (
        select(NotificationLog)
        .where(
            NotificationLog.user_id == user["user_id"],
            NotificationLog.tenant_id == user["tenant_id"],
            NotificationLog.read_at.is_(None),
        )
        .order_by(NotificationLog.created_at.desc())
    )
    return list(db.execute(q).scalars().all())


@router.get("/notifications/unread-count", response_model=None)
def unread_count(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Return badge count of unread notifications for the current user."""
    rows = db.execute(
        select(NotificationLog).where(
            NotificationLog.user_id == user["user_id"],
            NotificationLog.tenant_id == user["tenant_id"],
            NotificationLog.read_at.is_(None),
        )
    ).scalars().all()
    return {"count": len(rows)}


@router.post("/notifications/{notification_id}/read", response_model=None)
def mark_read(
    notification_id: UUID,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Mark a single notification as read."""
    row = db.execute(
        select(NotificationLog).where(
            NotificationLog.id == notification_id,
            NotificationLog.user_id == user["user_id"],
            NotificationLog.tenant_id == user["tenant_id"],
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    row.read_at = utcnow()
    db.commit()
    return {"status": "ok"}


@router.post("/notifications/read-all", response_model=None)
def mark_all_read(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Mark all unread notifications as read for the current user."""
    rows = db.execute(
        select(NotificationLog).where(
            NotificationLog.user_id == user["user_id"],
            NotificationLog.tenant_id == user["tenant_id"],
            NotificationLog.read_at.is_(None),
        )
    ).scalars().all()
    now = utcnow()
    for row in rows:
        row.read_at = now
    db.commit()
    return {"marked": len(rows)}


# ── Preference endpoints ───────────────────────────────────────────────────────


@router.get("/notifications/preferences", response_model=None)
def get_preferences(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[NotificationPreference]:
    """Return all notification preferences for the current user."""
    q = select(NotificationPreference).where(
        NotificationPreference.user_id == user["user_id"],
        NotificationPreference.tenant_id == user["tenant_id"],
    )
    return list(db.execute(q).scalars().all())


@router.patch("/notifications/preferences", response_model=None)
def upsert_preference(
    payload: PreferencePatch,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NotificationPreference:
    """Create or update a notification preference for the current user."""
    existing = db.execute(
        select(NotificationPreference).where(
            NotificationPreference.user_id == user["user_id"],
            NotificationPreference.tenant_id == user["tenant_id"],
            NotificationPreference.notification_type == payload.notification_type,
            NotificationPreference.channel == payload.channel,
        )
    ).scalar_one_or_none()

    if existing:
        existing.is_enabled = payload.is_enabled
        db.commit()
        db.refresh(existing)
        return existing

    pref = NotificationPreference(
        tenant_id=user["tenant_id"],
        user_id=user["user_id"],
        notification_type=payload.notification_type,
        channel=payload.channel,
        is_enabled=payload.is_enabled,
    )
    db.add(pref)
    db.commit()
    db.refresh(pref)
    return pref


# ── Device token endpoints ─────────────────────────────────────────────────────


@router.post("/devices/register", response_model=None)
def register_device(
    payload: DeviceRegister,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DeviceToken:
    """Register or reactivate a push notification device token."""
    existing = db.execute(
        select(DeviceToken).where(DeviceToken.token == payload.token)
    ).scalar_one_or_none()

    if existing:
        existing.is_active = True
        existing.user_id = user["user_id"]
        existing.tenant_id = user["tenant_id"]
        existing.platform = payload.platform
        db.commit()
        db.refresh(existing)
        return existing

    device = DeviceToken(
        tenant_id=user["tenant_id"],
        user_id=user["user_id"],
        platform=payload.platform,
        token=payload.token,
        is_active=True,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


@router.delete("/devices/{token}", response_model=None)
def deregister_device(
    token: str,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Deactivate a device push token for the current user."""
    device = db.execute(
        select(DeviceToken).where(
            DeviceToken.token == token,
            DeviceToken.user_id == user["user_id"],
            DeviceToken.tenant_id == user["tenant_id"],
        )
    ).scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device token not found")
    device.is_active = False
    db.commit()
    return {"status": "deregistered"}
