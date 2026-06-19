"""
Inbound communications router — webhook receivers for inbound SMS and email.

Two flows:
- Twilio inbound SMS: customer replies to an outbound SMS. Twilio POSTs a
  form-encoded payload to our webhook. We store it and return an empty 200 so
  Twilio treats the delivery as successful.
- Inbound email: mail provider (M365, Mailgun, SendGrid) POSTs a parsed JSON
  payload. We store it and route to the staff inbox.

Admin endpoints (list/retrieve/mark-read/link) are auth + module gated behind
`communications`. Webhook endpoints are PUBLIC — tenant is derived from a
required `?tenant=xxx` query param (Twilio only allows one global webhook URL
per account, so we disambiguate at the URL level).
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request

from gdx_dispatch.core.twilio_signature import verify_twilio_signature
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Routers — admin (gated) and public (webhooks, no auth)
# ---------------------------------------------------------------------------


admin_router = APIRouter(
    tags=["inbound_comms"],
    dependencies=[Depends(require_module("communications"))],
)

public_router = APIRouter(tags=["inbound_comms_public"])


# ---------------------------------------------------------------------------
# Models (inline — collections.py pattern)
# ---------------------------------------------------------------------------


from gdx_dispatch.models.tenant_models import InboundEmail, InboundSMS  # noqa: E402

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class InboundEmailWebhookIn(BaseModel):
    from_email: str = Field(min_length=3, max_length=254)
    from_name: str | None = Field(default=None, max_length=200)
    to_email: str = Field(min_length=3, max_length=254)
    subject: str | None = Field(default=None, max_length=500)
    body_text: str | None = Field(default=None, max_length=1_000_000)
    body_html: str | None = Field(default=None, max_length=2_000_000)
    message_id: str | None = Field(default=None, max_length=200)
    has_attachments: bool = False


class LinkEntityIn(BaseModel):
    customer_id: str | None = Field(default=None, max_length=64)
    job_id: str | None = Field(default=None, max_length=64)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_id(request: Request) -> str:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    tid = str(tenant.get("id") or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


def _user_id(user: Any) -> str:
    if not isinstance(user, dict):
        return "system"
    return str(user.get("sub") or user.get("user_id") or user.get("email") or "system")


def _parse_uuid_or_none(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid UUID: {value}") from exc


def _serialize_sms(row: InboundSMS) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "company_id": row.company_id,
        "from_number": row.from_number,
        "to_number": row.to_number,
        "body": row.body,
        "provider": row.provider,
        "provider_message_id": row.provider_message_id,
        "customer_id": str(row.customer_id) if row.customer_id else None,
        "job_id": str(row.job_id) if row.job_id else None,
        "processed_at": row.processed_at.isoformat() if row.processed_at else None,
        "received_at": row.received_at.isoformat() if row.received_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _serialize_email(row: InboundEmail) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "company_id": row.company_id,
        "from_email": row.from_email,
        "from_name": row.from_name,
        "to_email": row.to_email,
        "subject": row.subject,
        "body_text": row.body_text,
        "body_html": row.body_html,
        "provider": row.provider,
        "provider_message_id": row.provider_message_id,
        "customer_id": str(row.customer_id) if row.customer_id else None,
        "job_id": str(row.job_id) if row.job_id else None,
        "has_attachments": bool(row.has_attachments),
        "read_at": row.read_at.isoformat() if row.read_at else None,
        "received_at": row.received_at.isoformat() if row.received_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _audit(
    db: Session,
    *,
    tenant_id: str,
    user: Any,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=_user_id(user),
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception(
            "inbound_comms_audit_failed action=%s entity_id=%s", action, entity_id
        )
        db.rollback()


# ---------------------------------------------------------------------------
# Admin endpoints — SMS
# ---------------------------------------------------------------------------


@admin_router.get("/api/inbound-sms", response_model=None)
def list_inbound_sms(
    request: Request,
    from_number: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    stmt = select(InboundSMS).where(InboundSMS.company_id == tenant_id)
    if from_number:
        stmt = stmt.where(InboundSMS.from_number == from_number)
    stmt = stmt.order_by(InboundSMS.received_at.desc()).limit(limit).offset(offset)
    rows = db.execute(stmt).scalars().all()
    return [_serialize_sms(r) for r in rows]


@admin_router.get("/api/inbound-sms/{sms_id}", response_model=None)
def get_inbound_sms(
    sms_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = db.execute(
        select(InboundSMS).where(
            InboundSMS.id == sms_id,
            InboundSMS.company_id == tenant_id,
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Inbound SMS not found")
    return _serialize_sms(row)


@admin_router.post("/api/inbound-sms/{sms_id}/link", response_model=None)
def link_inbound_sms(
    sms_id: UUID,
    payload: LinkEntityIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = db.execute(
        select(InboundSMS).where(
            InboundSMS.id == sms_id,
            InboundSMS.company_id == tenant_id,
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Inbound SMS not found")

    customer_uuid = _parse_uuid_or_none(payload.customer_id)
    job_uuid = _parse_uuid_or_none(payload.job_id)
    if customer_uuid is not None:
        row.customer_id = customer_uuid
    if job_uuid is not None:
        row.job_id = job_uuid
    row.processed_at = utcnow()
    db.commit()
    db.refresh(row)

    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="inbound_sms_linked",
        entity_type="inbound_sms",
        entity_id=str(row.id),
        details={
            "customer_id": str(row.customer_id) if row.customer_id else None,
            "job_id": str(row.job_id) if row.job_id else None,
        },
        request=request,
    )
    return _serialize_sms(row)


# ---------------------------------------------------------------------------
# Admin endpoints — Email
# ---------------------------------------------------------------------------


@admin_router.get("/api/inbound-email", response_model=None)
def list_inbound_email(
    request: Request,
    unread_only: bool = Query(default=False),
    from_email: str | None = Query(default=None, max_length=254),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    stmt = select(InboundEmail).where(InboundEmail.company_id == tenant_id)
    if unread_only:
        stmt = stmt.where(InboundEmail.read_at.is_(None))
    if from_email:
        stmt = stmt.where(InboundEmail.from_email == from_email)
    stmt = stmt.order_by(InboundEmail.received_at.desc()).limit(limit).offset(offset)
    rows = db.execute(stmt).scalars().all()
    return [_serialize_email(r) for r in rows]


@admin_router.get("/api/inbound-email/{email_id}", response_model=None)
def get_inbound_email(
    email_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = db.execute(
        select(InboundEmail).where(
            InboundEmail.id == email_id,
            InboundEmail.company_id == tenant_id,
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Inbound email not found")
    return _serialize_email(row)


@admin_router.patch("/api/inbound-email/{email_id}/read", response_model=None)
def mark_inbound_email_read(
    email_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = db.execute(
        select(InboundEmail).where(
            InboundEmail.id == email_id,
            InboundEmail.company_id == tenant_id,
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Inbound email not found")
    if row.read_at is None:
        row.read_at = utcnow()
        db.commit()
        db.refresh(row)

    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="inbound_email_read",
        entity_type="inbound_email",
        entity_id=str(row.id),
        details={"from_email": row.from_email},
        request=request,
    )
    return _serialize_email(row)


@admin_router.post("/api/inbound-email/{email_id}/link", response_model=None)
def link_inbound_email(
    email_id: UUID,
    payload: LinkEntityIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = db.execute(
        select(InboundEmail).where(
            InboundEmail.id == email_id,
            InboundEmail.company_id == tenant_id,
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Inbound email not found")

    customer_uuid = _parse_uuid_or_none(payload.customer_id)
    job_uuid = _parse_uuid_or_none(payload.job_id)
    if customer_uuid is not None:
        row.customer_id = customer_uuid
    if job_uuid is not None:
        row.job_id = job_uuid
    db.commit()
    db.refresh(row)

    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="inbound_email_linked",
        entity_type="inbound_email",
        entity_id=str(row.id),
        details={
            "customer_id": str(row.customer_id) if row.customer_id else None,
            "job_id": str(row.job_id) if row.job_id else None,
        },
        request=request,
    )
    return _serialize_email(row)


# ---------------------------------------------------------------------------
# Public webhooks (no auth) — tenant from ?tenant= query param
# ---------------------------------------------------------------------------


def _require_tenant_param(tenant: str | None) -> str:
    tid = (tenant or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="Missing required tenant query param")
    if len(tid) > 64:
        raise HTTPException(status_code=400, detail="Invalid tenant query param")
    return tid


@public_router.post("/api/inbound-sms/webhook", response_model=None)
def twilio_inbound_sms_webhook(
    request: Request,
    _sig: None = Depends(verify_twilio_signature),
    tenant: str | None = Query(default=None, max_length=64),
    From: str = Form(..., min_length=1, max_length=30),
    To: str = Form(..., min_length=1, max_length=30),
    Body: str = Form(..., max_length=10000),
    MessageSid: str | None = Form(default=None, max_length=100),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _require_tenant_param(tenant)
    now = utcnow()
    row = InboundSMS(
        id=uuid4(),
        company_id=tenant_id,
        from_number=From,
        to_number=To,
        body=Body,
        provider="twilio",
        provider_message_id=MessageSid,
        received_at=now,
        created_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    _audit(
        db,
        tenant_id=tenant_id,
        user={"sub": "twilio-webhook"},
        action="inbound_sms_received",
        entity_type="inbound_sms",
        entity_id=str(row.id),
        details={
            "from_number": From,
            "to_number": To,
            "provider": "twilio",
            "provider_message_id": MessageSid,
        },
        request=request,
    )
    # Twilio requires a 2xx — empty body is fine (TwiML-compatible).
    return {}


@public_router.post("/api/inbound-email/webhook", response_model=None)
def inbound_email_webhook(
    payload: InboundEmailWebhookIn,
    request: Request,
    tenant: str | None = Query(default=None, max_length=64),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _require_tenant_param(tenant)
    now = utcnow()
    row = InboundEmail(
        id=uuid4(),
        company_id=tenant_id,
        from_email=payload.from_email,
        from_name=payload.from_name,
        to_email=payload.to_email,
        subject=payload.subject,
        body_text=payload.body_text,
        body_html=payload.body_html,
        provider="m365",
        provider_message_id=payload.message_id,
        has_attachments=bool(payload.has_attachments),
        received_at=now,
        created_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    _audit(
        db,
        tenant_id=tenant_id,
        user={"sub": "email-webhook"},
        action="inbound_email_received",
        entity_type="inbound_email",
        entity_id=str(row.id),
        details={
            "from_email": payload.from_email,
            "to_email": payload.to_email,
            "provider": "m365",
            "provider_message_id": payload.message_id,
        },
        request=request,
    )
    return {"status": "ok", "id": str(row.id)}
