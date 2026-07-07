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
    # PR6 (Doug 2026-07-07): automated dunning is OPT-IN, default OFF.
    auto_send_enabled: bool = False
    auto_send_nudge_dismissed: bool = False
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
        "auto_send_enabled": bool(getattr(s, "auto_send_enabled", False)),
        "auto_send_nudge_dismissed": bool(getattr(s, "auto_send_nudge_dismissed", False)),
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
    row.auto_send_enabled = payload.auto_send_enabled
    row.auto_send_nudge_dismissed = payload.auto_send_nudge_dismissed
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
            "auto_send_enabled": row.auto_send_enabled,
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

    # PR6: email reminders ACTUALLY SEND now (pre-fix this endpoint only
    # logged a row — theater). Non-email channels stay log-only: they're
    # "I called/texted them" records of a human action.
    sent = False
    skip_reason: str | None = None
    if data.channel == "email":
        from gdx_dispatch.models.tenant_models import Invoice
        invoice = db.execute(
            select(Invoice).where(Invoice.id == invoice_id, Invoice.deleted_at.is_(None))
        ).scalar_one_or_none()
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice not found")
        settings = _get_or_create_settings(db, tenant_id)
        sent, skip_reason = send_reminder_email_for_invoice(
            db, tenant_id, invoice, settings, user_id=_user_id(user)
        )

    delivery_suffix = None
    if data.channel == "email":
        delivery_suffix = "[delivered]" if sent else f"[skipped: {skip_reason}]"
    r = PaymentReminder(
        invoice_id=invoice_id,
        stage=data.stage,
        channel=data.channel,
        sent_at=utcnow(),
        sent_by=_user_id(user),
        notes=" ".join(x for x in (data.notes, delivery_suffix) if x) or None,
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
            "sent": sent,
            "skip_reason": skip_reason,
        },
        request=request,
    )
    out = _serialize_reminder(r)
    out["sent"] = sent
    out["skip_reason"] = skip_reason
    return out


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


# --------------------------------------------------------------------------- #
# PR6-billing-capture — real sending + the auto-dunning qualifier
# --------------------------------------------------------------------------- #


def _reminder_context(db: Session, invoice) -> dict[str, Any]:
    from datetime import UTC, date, datetime

    from gdx_dispatch.models.tenant_models import Customer

    customer = None
    if invoice.customer_id:
        customer = db.execute(
            select(Customer).where(Customer.id == invoice.customer_id)
        ).scalar_one_or_none()
    days_overdue = 0
    if invoice.due_date:
        days_overdue = max((date.today() - invoice.due_date).days, 0)
    _ = datetime.now(UTC)
    return {
        "customer": customer,
        "ctx": {
            "invoice_number": invoice.invoice_number or str(invoice.id)[:8],
            "customer_name": (customer.name if customer else None) or "Valued Customer",
            "amount_due": f"{float(invoice.balance_due or 0):.2f}",
            "days_overdue": days_overdue,
            "due_date": invoice.due_date.isoformat() if invoice.due_date else "",
        },
    }


def send_reminder_email_for_invoice(
    db: Session,
    tenant_id: str,
    invoice,
    settings: ReminderSettings,
    *,
    user_id: str | None = None,
) -> tuple[bool, str | None]:
    """Render the tenant's reminder template and ACTUALLY send it.

    Returns (sent, skip_reason). PR6: pre-fix, every reminder endpoint only
    logged a PaymentReminder row — a reminder no customer receives is
    theater. No email config / no customer email = a VISIBLE skip_reason,
    never a silent log row.
    """
    from gdx_dispatch.core.transactional_email import send_transactional_email

    bits = _reminder_context(db, invoice)
    customer = bits["customer"]
    if customer is None or not (customer.email or "").strip():
        return False, "no_recipient_email"
    subject = _render_template(settings.subject_template, bits["ctx"])
    body = _render_template(settings.body_template, bits["ctx"])
    html = "<p>" + body.replace("\n", "<br>") + "</p>"
    sent, _provider, skip_reason = send_transactional_email(
        tenant_db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        to_email=customer.email.strip(),
        to_name=customer.name or "",
        subject=subject,
        html_body=html,
    )
    return sent, skip_reason


def compute_due_sends(db: Session, settings: ReminderSettings) -> list[dict[str, Any]]:
    """Qualifier for automated dunning — shared by the beat task and the
    settings-screen preview so the operator sees EXACTLY who would get
    emailed before flipping auto-send on (Doug 2026-07-07).

    An invoice qualifies per-threshold: status='sent', balance_due>0, not
    dunning-paused, days_overdue >= T, and no PaymentReminder already
    recorded for that invoice at that threshold (threshold_days is the
    idempotency key; manual NULL-threshold logs never suppress)."""
    from datetime import date as _date

    from gdx_dispatch.models.tenant_models import Invoice

    try:
        thresholds = sorted({int(d) for d in json.loads(settings.schedule_days or "[]")})
    except (ValueError, TypeError):
        log.exception("reminder_schedule_days_parse_failed")
        thresholds = []
    if not thresholds:
        return []

    today = _date.today()
    overdue = db.execute(
        select(Invoice).where(
            Invoice.deleted_at.is_(None),
            Invoice.status == "sent",
            Invoice.balance_due > 0,
            Invoice.dunning_paused.is_(False),
            Invoice.due_date.is_not(None),
            Invoice.due_date < today,
        )
    ).scalars().all()
    if not overdue:
        return []

    # Highest threshold EVER recorded per invoice — the escalation floor.
    recorded_max: dict[Any, int] = {}
    for r in db.execute(
        select(PaymentReminder).where(
            PaymentReminder.invoice_id.in_([i.id for i in overdue]),
            PaymentReminder.threshold_days.is_not(None),
        )
    ).scalars().all():
        prev = recorded_max.get(r.invoice_id)
        if prev is None or int(r.threshold_days) > prev:
            recorded_max[r.invoice_id] = int(r.threshold_days)

    stage_names = ["friendly", "first_reminder", "second_reminder", "final_notice"]
    due: list[dict[str, Any]] = []
    for inv in overdue:
        days = (today - inv.due_date).days
        crossed = [t for t in thresholds if days >= t]
        if not crossed:
            continue
        # ESCALATION-ONLY: a threshold fires only if it is ABOVE the highest
        # ever recorded for this invoice. This is both the idempotency rule
        # and the anti-spam rule: the enable-day catch-up records t=30 and
        # the still-crossed lower stages (7, 14) can never fire afterward —
        # without the floor, a 40-day-overdue invoice would get three emails
        # on three consecutive days in DESCENDING severity (audit round 2
        # walk-through). One email per invoice per tick, always upward.
        floor = recorded_max.get(inv.id)
        eligible = [t for t in crossed if floor is None or t > floor]
        if not eligible:
            continue
        t = max(eligible)
        stage_idx = min(thresholds.index(t), len(stage_names) - 1)
        due.append({
            "invoice": inv,
            "threshold_days": t,
            "stage": stage_names[stage_idx],
            "days_overdue": days,
        })
    return due


@router.get("/api/invoice-reminders/auto-send-preview", response_model=None)
def auto_send_preview(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Who would get emailed if auto-send were ON right now — rendered on
    the settings screen BEFORE the operator flips the toggle."""
    tenant_id = _tenant_id(request)
    settings = _get_or_create_settings(db, tenant_id)
    due = compute_due_sends(db, settings)
    return {
        "count": len(due),
        "total_balance": round(sum(float(d["invoice"].balance_due or 0) for d in due), 2),
        "invoices": [
            {
                "invoice_id": str(d["invoice"].id),
                "invoice_number": d["invoice"].invoice_number,
                "balance_due": float(d["invoice"].balance_due or 0),
                "days_overdue": d["days_overdue"],
                "threshold_days": d["threshold_days"],
                "stage": d["stage"],
            }
            for d in due
        ],
    }


class DunningPauseIn(BaseModel):
    paused: bool


@router.post("/api/invoices/{invoice_id}/dunning-pause", response_model=None)
def set_dunning_pause(
    invoice_id: UUID,
    payload: DunningPauseIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """PR6 — the per-invoice dunning mute (Doug 2026-07-07: manual reminder
    logs never pause the robot; this explicit switch for real payment
    arrangements does). Works on SENT invoices — the invoice PATCH endpoint
    is deliberately draft-only, so this is its own verb. Office-only:
    pausing collections is a money decision, not a tech action."""
    from gdx_dispatch.core.permissions import is_dispatch_manager
    from gdx_dispatch.models.tenant_models import Invoice

    if not is_dispatch_manager(user):
        raise HTTPException(
            status_code=403,
            detail="pausing reminders requires dispatcher, admin, or owner role",
        )

    tenant_id = _tenant_id(request)
    invoice = db.execute(
        select(Invoice).where(Invoice.id == invoice_id, Invoice.deleted_at.is_(None))
    ).scalar_one_or_none()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice.dunning_paused = bool(payload.paused)
    db.commit()
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="invoice_dunning_pause" if payload.paused else "invoice_dunning_resume",
        entity_type="invoice",
        entity_id=str(invoice_id),
        details={"paused": bool(payload.paused)},
        request=request,
    )
    return {"id": str(invoice_id), "dunning_paused": bool(invoice.dunning_paused)}
