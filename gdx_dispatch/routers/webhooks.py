"""
Webhooks router — tenant-managed outbound webhook subscriptions.

Dealers hook Zapier/n8n/their own endpoint to receive events like
job.completed, invoice.paid, estimate.accepted. This sprint ships the
CRUD + a synchronous test-send endpoint so integrators can verify their
receiver. Async firing on real events is a separate worker sprint.

Gate: require_module("jobs") — events belong to the jobs domain.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    Uuid,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from gdx_dispatch.core.audit import TenantBase, log_audit_event_sync, utcnow
from gdx_dispatch.core.ssrf_guard import validate_outbound_url
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["webhooks"],
    dependencies=[Depends(require_module("jobs")), Depends(require_role("admin", "owner", "superadmin"))],
)


# ---------------------------------------------------------------------------
# Event catalogue
# ---------------------------------------------------------------------------

WEBHOOK_EVENTS = [
    "job.created", "job.updated", "job.completed", "job.cancelled",
    "estimate.sent", "estimate.accepted", "estimate.declined",
    "invoice.created", "invoice.sent", "invoice.paid", "invoice.overdue",
    "customer.created", "customer.updated",
    "payment.succeeded", "payment.failed",
    "appointment.scheduled", "appointment.confirmed", "appointment.completed",
]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class WebhookSubscription(TenantBase):
    __tablename__ = "webhook_subscriptions"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    secret: Mapped[str] = mapped_column(String(200), nullable=True)
    events: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookDeliveryLog(TenantBase):
    __tablename__ = "webhook_delivery_logs"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subscription_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    event: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    request_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    webhook_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class WebhookSubscriptionIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=8, max_length=2048, pattern=r"^https?://")
    secret: str | None = Field(default=None, max_length=200)
    events: list[str] = Field(min_length=1, max_length=50)
    active: bool = True

    @field_validator("events")
    @classmethod
    def _events_must_be_known(cls, v):
        unknown = [e for e in v if e not in WEBHOOK_EVENTS]
        if unknown:
            raise ValueError(f"unknown events: {unknown}")
        return list(dict.fromkeys(v))


class WebhookSubscriptionPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=8, max_length=2048, pattern=r"^https?://")
    secret: str | None = Field(default=None, max_length=200)
    events: list[str] | None = Field(default=None, max_length=50)
    active: bool | None = None

    @field_validator("events")
    @classmethod
    def _events_must_be_known(cls, v):
        if v is None:
            return v
        unknown = [e for e in v if e not in WEBHOOK_EVENTS]
        if unknown:
            raise ValueError(f"unknown events: {unknown}")
        return list(dict.fromkeys(v))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", None) or {}
    return str(tenant.get("id") or "")


def _user_id(user: Any) -> str:
    if isinstance(user, dict):
        return str(user.get("sub") or user.get("user_id") or "system")
    return "system"


def _serialize(s: WebhookSubscription) -> dict[str, Any]:
    try:
        events_list = json.loads(s.events) if s.events else []
    except json.JSONDecodeError:
        log.exception("_serialize_failed")
        events_list = []
    return {
        "id": str(s.id),
        "name": s.name,
        "url": s.url,
        "has_secret": bool(s.secret),
        "events": events_list,
        "active": s.active,
        "created_by": s.created_by,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _serialize_delivery(d: WebhookDeliveryLog) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "subscription_id": str(d.subscription_id),
        "event": d.event,
        "url": d.url,
        "response_status": d.response_status,
        "response_body": (d.response_body or "")[:500],
        "error": d.error,
        "duration_ms": d.duration_ms,
        "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None,
    }


def _get_scoped(db: Session, sub_id: UUID, tenant_id: str) -> WebhookSubscription:
    row = db.execute(
        select(WebhookSubscription).where(
            WebhookSubscription.id == sub_id,
            WebhookSubscription.company_id == tenant_id,
            WebhookSubscription.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook subscription not found")
    return row


def _audit(
    db: Session,
    *,
    tenant_id: str,
    user: Any,
    action: str,
    entity_id: str,
    details: dict | None = None,
    request: Request | None = None,
) -> None:
    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=_user_id(user),
            action=action,
            entity_type="webhook_subscription",
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("webhook_audit_failed")
        try:
            db.rollback()
        except Exception:
            log.exception("webhook_audit_rollback_failed")


# ---------------------------------------------------------------------------
# Endpoints — CRUD
# ---------------------------------------------------------------------------

@router.get("/api/webhooks/events", response_model=None)
def list_event_catalogue(_: dict = Depends(get_current_user)) -> list[str]:
    return list(WEBHOOK_EVENTS)


@router.get("/api/webhooks/subscriptions", response_model=None)
def list_subscriptions(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    active_only: bool = False,
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    stmt = select(WebhookSubscription).where(
        WebhookSubscription.company_id == tenant_id,
        WebhookSubscription.deleted_at.is_(None),
    )
    if active_only:
        stmt = stmt.where(WebhookSubscription.active.is_(True))
    stmt = stmt.order_by(WebhookSubscription.created_at.desc())
    rows = db.execute(stmt).scalars().all()
    return [_serialize(r) for r in rows]


@router.post("/api/webhooks/subscriptions", response_model=None, status_code=201)
def create_subscription(
    payload: WebhookSubscriptionIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = WebhookSubscription(
        company_id=tenant_id,
        name=payload.name,
        url=payload.url,
        secret=payload.secret,
        events=json.dumps(payload.events),
        active=payload.active,
        created_by=_user_id(user),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _audit(
        db, tenant_id=tenant_id, user=user,
        action="webhook_subscription_created",
        entity_id=str(row.id),
        details={"name": row.name, "events": payload.events},
        request=request,
    )
    return _serialize(row)


@router.get("/api/webhooks/subscriptions/{sub_id}", response_model=None)
def get_subscription(
    sub_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = _get_scoped(db, sub_id, tenant_id)
    return _serialize(row)


@router.patch("/api/webhooks/subscriptions/{sub_id}", response_model=None)
def update_subscription(
    sub_id: UUID,
    payload: WebhookSubscriptionPatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = _get_scoped(db, sub_id, tenant_id)
    if payload.name is not None:
        row.name = payload.name
    if payload.url is not None:
        row.url = payload.url
    if payload.secret is not None:
        row.secret = payload.secret
    if payload.events is not None:
        row.events = json.dumps(payload.events)
    if payload.active is not None:
        row.active = payload.active
    db.commit()
    db.refresh(row)
    _audit(
        db, tenant_id=tenant_id, user=user,
        action="webhook_subscription_updated",
        entity_id=str(sub_id),
        details={"fields": list(payload.model_dump(exclude_unset=True).keys())},
        request=request,
    )
    return _serialize(row)


@router.delete("/api/webhooks/subscriptions/{sub_id}", response_model=None, status_code=204)
def delete_subscription(
    sub_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    row = _get_scoped(db, sub_id, tenant_id)
    row.deleted_at = utcnow()
    db.commit()
    _audit(
        db, tenant_id=tenant_id, user=user,
        action="webhook_subscription_deleted",
        entity_id=str(sub_id),
        request=request,
    )
    return None


# ---------------------------------------------------------------------------
# Test send + delivery log
# ---------------------------------------------------------------------------

@router.post("/api/webhooks/subscriptions/{sub_id}/test", response_model=None)
def test_subscription(
    sub_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = _get_scoped(db, sub_id, tenant_id)

    payload = json.dumps({
        "event": "test.ping",
        "timestamp": utcnow().isoformat(),
        "tenant_id": tenant_id,
        "subscription_id": str(row.id),
    })
    payload_bytes = payload.encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "GDX-Webhooks/1.0",
    }
    if row.secret:
        sig = hmac.new(row.secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
        headers["X-GDX-Signature"] = f"sha256={sig}"

    status_code: int | None = None
    resp_body: str | None = None
    err: str | None = None
    t0 = time.time()
    try:
        validate_outbound_url(row.url)
        req = urllib.request.Request(row.url, data=payload_bytes, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            status_code = resp.status
            resp_body = resp.read(5000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        log.exception("test_subscription_failed")
        status_code = e.code
        try:
            resp_body = (e.read(5000) or b"").decode("utf-8", errors="replace")
        except Exception:
            log.exception("webhook_test_read_error_body_failed")
            resp_body = None
    except Exception as e:
        log.exception("webhook_test_send_failed")
        err = str(e)[:500]
    duration_ms = int((time.time() - t0) * 1000)

    delivery = WebhookDeliveryLog(
        company_id=tenant_id,
        subscription_id=row.id,
        event="test.ping",
        url=row.url,
        request_body=payload[:5000],
        response_status=status_code,
        response_body=(resp_body or "")[:5000] if resp_body else None,
        error=err,
        duration_ms=duration_ms,
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)

    _audit(
        db, tenant_id=tenant_id, user=user,
        action="webhook_subscription_tested",
        entity_id=str(sub_id),
        details={"status_code": status_code, "duration_ms": duration_ms, "error": err},
        request=request,
    )

    return {
        "delivery_id": str(delivery.id),
        "status_code": status_code,
        "duration_ms": duration_ms,
        "error": err,
    }


@router.get("/api/webhooks/subscriptions/{sub_id}/deliveries", response_model=None)
def list_deliveries(
    sub_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 50,
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    _get_scoped(db, sub_id, tenant_id)  # tenant scope check
    limit = max(1, min(int(limit or 50), 500))
    stmt = (
        select(WebhookDeliveryLog)
        .where(
            WebhookDeliveryLog.company_id == tenant_id,
            WebhookDeliveryLog.subscription_id == sub_id,
        )
        .order_by(WebhookDeliveryLog.delivered_at.desc())
        .limit(limit)
    )
    rows = db.execute(stmt).scalars().all()
    return [_serialize_delivery(r) for r in rows]
