from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import (
    Notification,
    NotificationSentHistory,
    NotificationSettings,
    NotificationTemplate,
)
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["notifications"],
    dependencies=[Depends(require_module("communications"))],
)

DEFAULT_TEMPLATE_KEYS = [
    "appointment_reminder_24h",
    "on_my_way",
    "job_completed",
    "review_request",
    "payment_received",
]


class NotificationSettingsPatch(BaseModel):
    email_enabled: bool | None = None
    sms_enabled: bool | None = None
    sender_name: str | None = None


class NotificationSettingsResponse(BaseModel):
    email_enabled: bool
    sms_enabled: bool
    sender_name: str


class NotificationTemplateCreate(BaseModel):
    key: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)


class NotificationTemplateResponse(BaseModel):
    id: str
    key: str
    subject: str
    body: str
    is_default: bool
    created_at: str


class NotificationSendRequest(BaseModel):
    customer_id: str = Field(min_length=1)
    template_key: str = Field(min_length=1)
    channel: str = Field(default="sms", pattern="^(sms|email)$")
    manual_message: str | None = None


class NotificationSendResponse(BaseModel):
    id: str
    status: str
    customer_id: str
    channel: str
    rendered_message: str
    sent_at: str


class NotificationHistoryItem(BaseModel):
    id: str
    customer_id: str
    template_key: str
    channel: str
    status: str
    rendered_message: str
    sent_at: str


class NotificationHistoryResponse(BaseModel):
    page: int
    page_size: int
    total: int
    items: list[NotificationHistoryItem]


def _tenant_id(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id") or "")


def _user_id(current_user: Any) -> str:
    user = current_user or {}
    return str(user.get("user_id") or user.get("sub") or "system")



def _ensure_default_templates(db: Session, tenant_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    for key in DEFAULT_TEMPLATE_KEYS:
        existing = db.execute(
            select(NotificationTemplate.id).where(
                NotificationTemplate.tenant_id == tenant_id,
                NotificationTemplate.template_key == key,
            ).limit(1)
        ).scalar()
        if existing:
            continue
        tpl = NotificationTemplate(
            id=str(uuid4()),
            tenant_id=tenant_id,
            template_key=key,
            subject=key.replace("_", " ").title(),
            body=f"Default template for {key}",
            is_default=1,
            created_at=now,
        )
        db.add(tpl)
    db.commit()


@router.get("/api/notifications/settings", response_model=NotificationSettingsResponse)
def get_notification_settings(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NotificationSettingsResponse:
    _ = current_user
    tenant_id = _tenant_id(request)
    try:

        row = db.execute(
            select(NotificationSettings).where(
                NotificationSettings.tenant_id == tenant_id
            )
        ).scalars().first()
        if not row:
            now = datetime.now(UTC).isoformat()
            settings = NotificationSettings(
                tenant_id=tenant_id,
                email_enabled=1,
                sms_enabled=1,
                sender_name="Dispatch Team",
                updated_at=now,
            )
            db.add(settings)
            db.commit()
            return NotificationSettingsResponse(email_enabled=True, sms_enabled=True, sender_name="Dispatch Team")

        return NotificationSettingsResponse(
            email_enabled=bool(row.email_enabled),
            sms_enabled=bool(row.sms_enabled),
            sender_name=str(row.sender_name),
        )
    except SQLAlchemyError:
        log.exception("notifications_settings_get_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to fetch notification settings") from None


@router.patch("/api/notifications/settings", response_model=NotificationSettingsResponse)
def patch_notification_settings(
    payload: NotificationSettingsPatch,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NotificationSettingsResponse:
    tenant_id = _tenant_id(request)
    try:

        current = get_notification_settings(request=request, current_user=current_user, db=db)

        row = db.execute(
            select(NotificationSettings).where(
                NotificationSettings.tenant_id == tenant_id
            )
        ).scalars().first()

        if row:
            row.email_enabled = int(payload.email_enabled if payload.email_enabled is not None else current.email_enabled)
            row.sms_enabled = int(payload.sms_enabled if payload.sms_enabled is not None else current.sms_enabled)
            row.sender_name = payload.sender_name if payload.sender_name is not None else current.sender_name
            row.updated_at = datetime.now(UTC).isoformat()
        db.commit()

        updated = {
            "email_enabled": row.email_enabled if row else 1,
            "sms_enabled": row.sms_enabled if row else 1,
            "sender_name": row.sender_name if row else "Dispatch Team",
            "updated_at": row.updated_at if row else datetime.now(UTC).isoformat(),
        }

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=_user_id(current_user),
                action="notification_settings_updated",
                entity_type="notification_settings",
                entity_id=tenant_id,
                details=updated,
                request=request,
            )
        )
        db.commit()

        return NotificationSettingsResponse(
            email_enabled=bool(updated["email_enabled"]),
            sms_enabled=bool(updated["sms_enabled"]),
            sender_name=str(updated["sender_name"]),
        )
    except SQLAlchemyError:
        db.rollback()
        log.exception("notifications_settings_patch_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to update notification settings") from None


@router.get("/api/notifications/templates", response_model=list[NotificationTemplateResponse])
def list_notification_templates(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[NotificationTemplateResponse]:
    _ = current_user
    tenant_id = _tenant_id(request)
    try:

        _ensure_default_templates(db, tenant_id)
        rows = db.execute(
            select(NotificationTemplate).where(
                NotificationTemplate.tenant_id == tenant_id
            ).order_by(
                NotificationTemplate.is_default.desc(),
                NotificationTemplate.created_at.asc(),
            )
        ).scalars().all()
        return [
            NotificationTemplateResponse(
                id=str(row.id),
                key=str(row.template_key),
                subject=str(row.subject),
                body=str(row.body),
                is_default=bool(row.is_default),
                created_at=str(row.created_at),
            )
            for row in rows
        ]
    except SQLAlchemyError:
        log.exception("notifications_templates_list_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to list templates") from None


@router.post("/api/notifications/templates", response_model=NotificationTemplateResponse, status_code=201)
def create_notification_template(
    payload: NotificationTemplateCreate,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NotificationTemplateResponse:
    tenant_id = _tenant_id(request)
    template_id = str(uuid4())
    created_at = datetime.now(UTC).isoformat()
    try:

        tpl = NotificationTemplate(
            id=template_id,
            tenant_id=tenant_id,
            template_key=payload.key,
            subject=payload.subject,
            body=payload.body,
            is_default=0,
            created_at=created_at,
        )
        db.add(tpl)
        db.commit()

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=_user_id(current_user),
                action="notification_template_created",
                entity_type="notification_template",
                entity_id=template_id,
                details=payload.model_dump(),
                request=request,
            )
        )
        db.commit()

        return NotificationTemplateResponse(
            id=template_id,
            key=payload.key,
            subject=payload.subject,
            body=payload.body,
            is_default=False,
            created_at=created_at,
        )
    except SQLAlchemyError:
        db.rollback()
        log.exception("notifications_template_create_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to create template") from None


@router.post("/api/notifications/send", response_model=NotificationSendResponse, status_code=201)
def send_notification(
    payload: NotificationSendRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NotificationSendResponse:
    tenant_id = _tenant_id(request)
    send_id = str(uuid4())
    sent_at = datetime.now(UTC).isoformat()
    try:

        _ensure_default_templates(db, tenant_id)
        tpl = db.execute(
            select(NotificationTemplate.body).where(
                NotificationTemplate.tenant_id == tenant_id,
                NotificationTemplate.template_key == payload.template_key,
            ).limit(1)
        ).scalar()
        if not tpl:
            raise HTTPException(status_code=404, detail="Template not found")

        rendered = payload.manual_message or str(tpl)
        history = NotificationSentHistory(
            id=send_id,
            tenant_id=tenant_id,
            customer_id=payload.customer_id,
            template_key=payload.template_key,
            channel=payload.channel,
            status="sent",
            rendered_message=rendered,
            sent_at=sent_at,
        )
        db.add(history)
        db.commit()

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=_user_id(current_user),
                action="notification_sent",
                entity_type="notification",
                entity_id=send_id,
                details=payload.model_dump(),
                request=request,
            )
        )
        db.commit()

        return NotificationSendResponse(
            id=send_id,
            status="sent",
            customer_id=payload.customer_id,
            channel=payload.channel,
            rendered_message=rendered,
            sent_at=sent_at,
        )
    except SQLAlchemyError:
        db.rollback()
        log.exception("notifications_send_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to send notification") from None


@router.get("/api/notifications/history", response_model=NotificationHistoryResponse)
def list_notification_history(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NotificationHistoryResponse:
    _ = current_user
    tenant_id = _tenant_id(request)
    try:

        offset = (page - 1) * page_size
        total = int(
            db.execute(
                select(func.count()).select_from(NotificationSentHistory).where(
                    NotificationSentHistory.tenant_id == tenant_id
                )
            ).scalar()
            or 0
        )
        rows = db.execute(
            select(NotificationSentHistory).where(
                NotificationSentHistory.tenant_id == tenant_id
            ).order_by(
                NotificationSentHistory.sent_at.desc()
            ).limit(page_size).offset(offset)
        ).scalars().all()
        return NotificationHistoryResponse(
            page=page,
            page_size=page_size,
            total=total,
            items=[
                NotificationHistoryItem(
                    id=str(row.id),
                    customer_id=str(row.customer_id),
                    template_key=str(row.template_key),
                    channel=str(row.channel),
                    status=str(row.status),
                    rendered_message=str(row.rendered_message),
                    sent_at=str(row.sent_at),
                )
                for row in rows
            ],
        )
    except SQLAlchemyError:
        log.exception("notifications_history_list_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to fetch notification history") from None


# ── In-app notification endpoints (badge count, list, mark-read) ──────────


class NotificationCountResponse(BaseModel):
    count: int


class NotificationItem(BaseModel):
    id: str
    title: str
    message: str
    category: str
    is_read: bool
    created_at: str


class NotificationListResponse(BaseModel):
    page: int
    page_size: int
    total: int
    items: list[NotificationItem]



@router.get("/api/notifications/count", response_model=NotificationCountResponse)
def get_notification_count(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NotificationCountResponse:
    """Return count of unread in-app notifications for the current user."""
    tenant_id = _tenant_id(request)
    uid = _user_id(current_user)
    try:

        count = int(
            db.execute(
                select(func.count()).select_from(Notification).where(
                    Notification.tenant_id == tenant_id,
                    or_(Notification.user_id == uid, Notification.user_id.is_(None)),
                    Notification.is_read == 0,
                    Notification.deleted_at.is_(None),
                )
            ).scalar()
            or 0
        )
        return NotificationCountResponse(count=count)
    except SQLAlchemyError:
        log.exception("notifications_count_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to fetch notification count") from None


@router.get("/api/notifications", response_model=NotificationListResponse)
def list_notifications(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NotificationListResponse:
    """Return paginated list of in-app notifications for the current user."""
    tenant_id = _tenant_id(request)
    uid = _user_id(current_user)
    try:

        offset = (page - 1) * page_size
        base_filter = [
            Notification.tenant_id == tenant_id,
            or_(Notification.user_id == uid, Notification.user_id.is_(None)),
            Notification.deleted_at.is_(None),
        ]
        total = int(
            db.execute(
                select(func.count()).select_from(Notification).where(*base_filter)
            ).scalar()
            or 0
        )
        rows = db.execute(
            select(Notification).where(*base_filter).order_by(
                Notification.created_at.desc()
            ).limit(page_size).offset(offset)
        ).scalars().all()
        return NotificationListResponse(
            page=page,
            page_size=page_size,
            total=total,
            items=[
                NotificationItem(
                    id=str(row.id),
                    title=str(row.title),
                    message=str(row.message),
                    category=str(row.category),
                    is_read=bool(row.is_read),
                    created_at=str(row.created_at),
                )
                for row in rows
            ],
        )
    except SQLAlchemyError:
        log.exception("notifications_list_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to fetch notifications") from None


@router.post("/api/notifications/{notification_id}/read", status_code=200)
def mark_notification_read(
    notification_id: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Mark a single notification as read."""
    tenant_id = _tenant_id(request)
    uid = _user_id(current_user)
    try:

        notif = db.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.tenant_id == tenant_id,
                or_(Notification.user_id == uid, Notification.user_id.is_(None)),
                Notification.deleted_at.is_(None),
            )
        ).scalars().first()
        if not notif:
            raise HTTPException(status_code=404, detail="Notification not found")

        notif.is_read = 1
        db.commit()

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=uid,
                action="notification_marked_read",
                entity_type="notification",
                entity_id=notification_id,
                details={"notification_id": notification_id},
                request=request,
            )
        )
        db.commit()

        return {"status": "ok"}
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        log.exception("notifications_mark_read_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to mark notification as read") from None


@router.delete("/api/notifications/{notification_id}", status_code=200)
def delete_notification(
    notification_id: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Soft-delete a single notification (sets deleted_at; house pattern).

    Deleting a broadcast row (user_id NULL) removes it for the whole tenant —
    intended: single-tenant shop, "delete" means the office handled it.
    """
    tenant_id = _tenant_id(request)
    uid = _user_id(current_user)
    try:
        notif = db.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.tenant_id == tenant_id,
                or_(Notification.user_id == uid, Notification.user_id.is_(None)),
                Notification.deleted_at.is_(None),
            )
        ).scalars().first()
        if not notif:
            raise HTTPException(status_code=404, detail="Notification not found")

        notif.deleted_at = datetime.now(UTC).isoformat()
        db.commit()

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=uid,
                action="notification_deleted",
                entity_type="notification",
                entity_id=notification_id,
                details={"notification_id": notification_id},
                request=request,
            )
        )
        db.commit()

        return {"status": "ok"}
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        log.exception("notifications_delete_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to delete notification") from None


@router.delete("/api/notifications", status_code=200)
def clear_notifications(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Soft-delete every notification visible to the current user
    ("Clear all" in the drawer). Returns how many were cleared."""
    tenant_id = _tenant_id(request)
    uid = _user_id(current_user)
    try:
        now = datetime.now(UTC).isoformat()
        result = db.execute(
            sa_update(Notification)
            .where(
                Notification.tenant_id == tenant_id,
                or_(Notification.user_id == uid, Notification.user_id.is_(None)),
                Notification.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )
        db.commit()
        cleared = int(result.rowcount or 0)

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=uid,
                action="notifications_cleared",
                entity_type="notification",
                entity_id="all",
                details={"cleared": cleared},
                request=request,
            )
        )
        db.commit()

        return {"cleared": cleared}
    except SQLAlchemyError:
        db.rollback()
        log.exception("notifications_clear_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to clear notifications") from None
