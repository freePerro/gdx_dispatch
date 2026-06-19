"""
Invoice Reminders router — lightweight per-tenant reminder scheduling.

Distinct from gdx_dispatch/routers/collections.py which owns the full dunning workflow.
This router manages ReminderSettings (per-tenant schedule + templates),
manual reminder sending (recorded as PaymentReminder rows — reusing the
collections model), per-invoice history lookup, and template preview rendering.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.collections import PaymentReminder

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["invoice_reminders"],
    dependencies=[Depends(require_module("invoices")), Depends(require_role("admin", "owner", "superadmin"))],
)


REMINDER_STAGES = ("friendly", "first_reminder", "second_reminder", "final_notice", "collections")
REMINDER_CHANNELS = ("email", "sms", "call", "letter")


from gdx_dispatch.models.tenant_models import ReminderSettings  # noqa: E402

# --------------------------------------------------------------------------- #
# Pydantic schemas
# --------------------------------------------------------------------------- #


_DEFAULT_SUBJECT = "Payment reminder for invoice {invoice_number}"
_DEFAULT_BODY = (
    "Hi {customer_name},\n\nThis is a friendly reminder that invoice "
    "{invoice_number} for ${amount_due} is now {days_overdue} days overdue. "
    "Please remit payment at your earliest convenience.\n\nDue date: "
    "{due_date}\n\nThank you."
)


class ReminderSettingsIn(BaseModel):
    enabled: bool = True
    schedule_days: list[int] = Field(default_factory=lambda: [7, 14, 30], max_length=20)
    subject_template: str = Field(default=_DEFAULT_SUBJECT, min_length=1, max_length=500)
    body_template: str = Field(default=_DEFAULT_BODY, min_length=1, max_length=10000)

    @field_validator("schedule_days")
    @classmethod
    def _validate_days(cls, v: list[int]) -> list[int]:
        if not all(0 <= d <= 365 for d in v):
            raise ValueError("schedule_days must be 0-365")
        return sorted(set(v))


class SendReminderIn(BaseModel):
    channel: str = Field(default="email", pattern=r"^(email|sms|call|letter)$")
    stage: str = Field(
        default="friendly",
        pattern=r"^(friendly|first_reminder|second_reminder|final_notice|collections)$",
    )
    notes: str | None = Field(default=None, max_length=2000)


class PreviewIn(BaseModel):
    invoice_number: str = Field(min_length=1, max_length=100)
    customer_name: str = Field(min_length=1, max_length=200)
    amount_due: float = Field(ge=0, le=10_000_000)
    days_overdue: int = Field(ge=0, le=3650)
    due_date: str = Field(min_length=1, max_length=32)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


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


def _serialize_settings(s: ReminderSettings) -> dict[str, Any]:
    try:
        days = json.loads(s.schedule_days) if s.schedule_days else []
        if not isinstance(days, list):
            days = []
    except (ValueError, TypeError):
        log.exception("reminder_settings_schedule_days_parse_failed id=%s", s.id)
        days = []
    return {
        "id": str(s.id),
        "company_id": s.company_id,
        "enabled": bool(s.enabled),
        "schedule_days": days,
        "subject_template": s.subject_template,
        "body_template": s.body_template,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _serialize_reminder(r: PaymentReminder) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "invoice_id": str(r.invoice_id),
        "customer_id": str(r.customer_id) if r.customer_id else None,
        "customer_name": r.customer_name,
        "stage": r.stage,
        "channel": r.channel,
        "sent_at": r.sent_at.isoformat() if r.sent_at else None,
        "sent_by": r.sent_by,
        "notes": r.notes,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _get_or_create_settings(db: Session, tenant_id: str) -> ReminderSettings:
    row = db.execute(
        select(ReminderSettings).where(ReminderSettings.company_id == tenant_id)
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = ReminderSettings(
        company_id=tenant_id,
        enabled=True,
        schedule_days="[7,14,30]",
        subject_template=_DEFAULT_SUBJECT,
        body_template=_DEFAULT_BODY,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _render_template(template: str, ctx: dict[str, Any]) -> str:
    """Safe template render using format_map with defaultdict fallback.

    Unknown placeholders render as empty strings so a stale/bad template
    never raises KeyError at send time.
    """
    safe_ctx: defaultdict[str, Any] = defaultdict(str)
    for k, v in ctx.items():
        safe_ctx[k] = v
    try:
        return str(template).format_map(safe_ctx)
    except (IndexError, ValueError):
        # Bad format spec (e.g. `{0}` or `{x:.2z}`) — log and fall back to raw.
        log.exception("reminder_template_render_failed")
        return str(template)


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
            "invoice_reminder_audit_failed action=%s entity_id=%s", action, entity_id
        )
        db.rollback()


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get("/api/invoice-reminders/settings", response_model=None)
def get_settings(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = _get_or_create_settings(db, tenant_id)
    return _serialize_settings(row)


@router.post("/api/invoice-reminders/settings", response_model=None)
def update_settings(
    payload: ReminderSettingsIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = _get_or_create_settings(db, tenant_id)
    row.enabled = payload.enabled
    row.schedule_days = json.dumps(payload.schedule_days)
    row.subject_template = payload.subject_template
    row.body_template = payload.body_template
    db.commit()
    db.refresh(row)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="reminder_settings_updated",
        entity_type="reminder_settings",
        entity_id=str(row.id),
        details={
            "enabled": row.enabled,
            "schedule_days": payload.schedule_days,
        },
        request=request,
    )
    return _serialize_settings(row)


@router.post(
    "/api/invoices/{invoice_id}/send-reminder",
    response_model=None,
    status_code=201,
)
def send_reminder(
    invoice_id: UUID,
    request: Request,
    payload: SendReminderIn | None = None,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    data = payload or SendReminderIn()
    if data.stage not in REMINDER_STAGES:
        raise HTTPException(status_code=422, detail="Invalid stage")
    if data.channel not in REMINDER_CHANNELS:
        raise HTTPException(status_code=422, detail="Invalid channel")
    r = PaymentReminder(
        invoice_id=invoice_id,
        stage=data.stage,
        channel=data.channel,
        sent_at=utcnow(),
        sent_by=_user_id(user),
        notes=data.notes,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="reminder_sent",
        entity_type="invoice_reminder",
        entity_id=str(r.id),
        details={
            "invoice_id": str(invoice_id),
            "stage": data.stage,
            "channel": data.channel,
        },
        request=request,
    )
    return _serialize_reminder(r)


@router.get(
    "/api/invoices/{invoice_id}/reminder-history",
    response_model=None,
)
def reminder_history(
    invoice_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    # Tenant isolation is provided by get_db (database-per-tenant).
    # Still ensure the request carries tenant context before querying.
    _tenant_id(request)
    stmt = (
        select(PaymentReminder)
        .where(PaymentReminder.invoice_id == invoice_id)
        .order_by(PaymentReminder.created_at.desc())
    )
    rows = db.execute(stmt).scalars().all()
    return [_serialize_reminder(r) for r in rows]


@router.post("/api/invoice-reminders/preview", response_model=None)
def preview_reminder(
    payload: PreviewIn,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    settings = _get_or_create_settings(db, tenant_id)
    ctx = {
        "invoice_number": payload.invoice_number,
        "customer_name": payload.customer_name,
        "amount_due": f"{payload.amount_due:.2f}",
        "days_overdue": payload.days_overdue,
        "due_date": payload.due_date,
    }
    return {
        "subject": _render_template(settings.subject_template, ctx),
        "body": _render_template(settings.body_template, ctx),
    }
