from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import AutomationEnrollment, AutomationSequence, AutomationStep
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)


def _audit_ids(user: dict, request: Request | None) -> tuple[str, str]:
    tenant_id = ""
    if request is not None:
        tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
        tenant_id = str(tenant.get("id") or "")
    user_id = str(user.get("sub") or user.get("user_id") or user.get("id") or "system")
    return tenant_id, user_id

router = APIRouter(prefix="/api/automations", tags=["automations"], dependencies=[Depends(require_module("automations"))])

TriggerEvent = Literal["job_completed", "estimate_sent", "invoice_overdue", "customer_created"]
ActionType = Literal["send_email", "send_sms", "create_task", "update_status", "wait"]


class StepCreateIn(BaseModel):
    action_type: ActionType
    delay_hours: int = Field(default=0, ge=0)
    template: str = Field(default="", max_length=10000)


class SequenceCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    trigger_event: TriggerEvent
    steps: list[StepCreateIn] = Field(default_factory=list, max_length=100)


class SequencePatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    trigger_event: TriggerEvent | None = None
    is_active: bool | None = None


class StepOut(BaseModel):
    id: str
    sequence_id: str
    step_order: int
    action_type: ActionType
    delay_hours: int
    template: str
    created_at: str | None = None


class SequenceOut(BaseModel):
    id: str
    name: str
    trigger_event: TriggerEvent
    is_active: bool
    is_paused: bool
    created_at: str | None = None
    updated_at: str | None = None
    deleted_at: str | None = None
    steps: list[StepOut] = Field(default_factory=list)


class EnrollmentOut(BaseModel):
    id: str
    sequence_id: str
    entity_type: str
    entity_id: str
    status: str
    current_step: int
    enrolled_at: str | None = None
    next_run_at: str | None = None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _serialize_step(step: AutomationStep) -> dict[str, object]:
    return {
        "id": str(step.id),
        "sequence_id": str(step.sequence_id),
        "step_order": step.step_order,
        "action_type": step.action_type,
        "delay_hours": step.delay_hours,
        "template": step.template,
        "created_at": _iso(step.created_at),
    }


def _serialize_sequence(sequence: AutomationSequence, steps: list[AutomationStep]) -> dict[str, object]:
    return {
        "id": str(sequence.id),
        "name": sequence.name,
        "trigger_event": sequence.trigger_event,
        "is_active": bool(sequence.is_active),
        "is_paused": bool(sequence.is_paused),
        "created_at": _iso(sequence.created_at),
        "updated_at": _iso(sequence.updated_at),
        "deleted_at": _iso(sequence.deleted_at),
        "steps": [_serialize_step(step) for step in steps],
    }


def _serialize_enrollment(enrollment: AutomationEnrollment) -> dict[str, object]:
    return {
        "id": str(enrollment.id),
        "sequence_id": str(enrollment.sequence_id),
        "entity_type": enrollment.entity_type,
        "entity_id": enrollment.entity_id,
        "status": enrollment.status,
        "current_step": enrollment.current_step,
        "enrolled_at": _iso(enrollment.enrolled_at),
        "next_run_at": _iso(enrollment.next_run_at),
    }


def _get_sequence_or_404(automation_id: UUID, db: Session) -> AutomationSequence:
    sequence = db.execute(
        select(AutomationSequence).where(
            AutomationSequence.id == automation_id,
            AutomationSequence.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not sequence:
        raise HTTPException(status_code=404, detail="Automation sequence not found")
    return sequence


@router.get("", response_model=None)
def list_automations(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    sequences = db.execute(
        select(AutomationSequence)
        .where(AutomationSequence.deleted_at.is_(None))
        .order_by(AutomationSequence.name.asc())
    ).scalars().all()

    payload: list[dict[str, object]] = []
    for sequence in sequences:
        steps = db.execute(
            select(AutomationStep)
            .where(AutomationStep.sequence_id == sequence.id)
            .order_by(AutomationStep.step_order.asc(), AutomationStep.created_at.asc())
        ).scalars().all()
        payload.append(_serialize_sequence(sequence, steps))
    return payload


@router.post("", response_model=None, status_code=201)
def create_automation(
    payload: SequenceCreateIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    sequence = AutomationSequence(
        name=payload.name.strip(),
        trigger_event=payload.trigger_event,
        is_active=True,
        is_paused=False,
    )
    db.add(sequence)
    db.flush()

    created_steps: list[AutomationStep] = []
    for idx, step in enumerate(payload.steps, start=1):
        row = AutomationStep(
            sequence_id=sequence.id,
            step_order=idx,
            action_type=step.action_type,
            delay_hours=step.delay_hours,
            template=step.template,
        )
        db.add(row)
        created_steps.append(row)

    db.commit()
    db.refresh(sequence)
    for row in created_steps:
        db.refresh(row)

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="automation_created",
        entity_type="automation_sequence",
        entity_id=str(sequence.id),
        details={"name": sequence.name, "trigger_event": sequence.trigger_event, "step_count": len(created_steps)},
        request=request,
    )
    db.commit()
    return _serialize_sequence(sequence, created_steps)


@router.get("/{automation_id}", response_model=None)
def get_automation(
    automation_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    sequence = _get_sequence_or_404(automation_id, db)
    steps = db.execute(
        select(AutomationStep)
        .where(AutomationStep.sequence_id == sequence.id)
        .order_by(AutomationStep.step_order.asc(), AutomationStep.created_at.asc())
    ).scalars().all()
    return _serialize_sequence(sequence, steps)


@router.patch("/{automation_id}", response_model=None)
def patch_automation(
    automation_id: UUID,
    payload: SequencePatchIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    sequence = _get_sequence_or_404(automation_id, db)

    updates = payload.model_dump(exclude_unset=True)
    if "name" in updates and updates["name"] is not None:
        sequence.name = updates["name"].strip()
    if "trigger_event" in updates:
        sequence.trigger_event = updates["trigger_event"]
    if "is_active" in updates:
        sequence.is_active = bool(updates["is_active"])
    sequence.updated_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(sequence)

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="automation_updated",
        entity_type="automation_sequence",
        entity_id=str(sequence.id),
        details={"changed": list(updates.keys())},
        request=request,
    )
    db.commit()

    steps = db.execute(
        select(AutomationStep)
        .where(AutomationStep.sequence_id == sequence.id)
        .order_by(AutomationStep.step_order.asc(), AutomationStep.created_at.asc())
    ).scalars().all()
    return _serialize_sequence(sequence, steps)


@router.delete("/{automation_id}", response_model=None)
def delete_automation(
    automation_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    sequence = _get_sequence_or_404(automation_id, db)
    sequence.deleted_at = datetime.now(timezone.utc)
    sequence.is_active = False
    db.commit()

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="automation_deleted",
        entity_type="automation_sequence",
        entity_id=str(sequence.id),
        details={"name": sequence.name, "soft_delete": True},
        request=request,
    )
    db.commit()
    return {"deleted": True}


@router.post("/{automation_id}/steps", response_model=None, status_code=201)
def add_step(
    automation_id: UUID,
    payload: StepCreateIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    sequence = _get_sequence_or_404(automation_id, db)
    if sequence.is_paused:
        raise HTTPException(status_code=409, detail="Cannot add steps while sequence is paused")

    max_step = db.execute(
        select(func.max(AutomationStep.step_order)).where(AutomationStep.sequence_id == sequence.id)
    ).scalar_one_or_none()
    step = AutomationStep(
        sequence_id=sequence.id,
        step_order=int(max_step or 0) + 1,
        action_type=payload.action_type,
        delay_hours=payload.delay_hours,
        template=payload.template,
    )
    db.add(step)
    db.commit()
    db.refresh(step)

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="automation_step_added",
        entity_type="automation_step",
        entity_id=str(step.id),
        details={"sequence_id": str(sequence.id), "action_type": step.action_type, "step_order": step.step_order},
        request=request,
    )
    db.commit()
    return _serialize_step(step)


@router.get("/{automation_id}/enrollments", response_model=None)
def list_enrollments(
    automation_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    sequence = _get_sequence_or_404(automation_id, db)
    rows = db.execute(
        select(AutomationEnrollment)
        .where(
            AutomationEnrollment.sequence_id == sequence.id,
            AutomationEnrollment.status == "active",
        )
        .order_by(AutomationEnrollment.enrolled_at.desc())
    ).scalars().all()
    return [_serialize_enrollment(row) for row in rows]


@router.post("/{automation_id}/pause", response_model=None)
def pause_automation(
    automation_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    sequence = _get_sequence_or_404(automation_id, db)
    sequence.is_paused = True
    sequence.updated_at = datetime.now(timezone.utc)

    rows = db.execute(
        select(AutomationEnrollment).where(
            AutomationEnrollment.sequence_id == sequence.id,
            AutomationEnrollment.status == "active",
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    paused_count = 0
    for row in rows:
        row.status = "paused"
        row.paused_at = now
        paused_count += 1

    db.commit()
    db.refresh(sequence)

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="automation_paused",
        entity_type="automation_sequence",
        entity_id=str(sequence.id),
        details={"enrollments_paused": paused_count},
        request=request,
    )
    db.commit()

    steps = db.execute(
        select(AutomationStep)
        .where(AutomationStep.sequence_id == sequence.id)
        .order_by(AutomationStep.step_order.asc(), AutomationStep.created_at.asc())
    ).scalars().all()
    return _serialize_sequence(sequence, steps)


@router.post("/{automation_id}/resume", response_model=None)
def resume_automation(
    automation_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    sequence = _get_sequence_or_404(automation_id, db)
    sequence.is_paused = False
    sequence.updated_at = datetime.now(timezone.utc)

    rows = db.execute(
        select(AutomationEnrollment).where(
            AutomationEnrollment.sequence_id == sequence.id,
            AutomationEnrollment.status == "paused",
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    resumed_count = 0
    for row in rows:
        row.status = "active"
        row.resumed_at = now
        resumed_count += 1

    db.commit()
    db.refresh(sequence)

    tenant_id, user_id = _audit_ids(user, request)
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="automation_resumed",
        entity_type="automation_sequence",
        entity_id=str(sequence.id),
        details={"enrollments_resumed": resumed_count},
        request=request,
    )
    db.commit()

    steps = db.execute(
        select(AutomationStep)
        .where(AutomationStep.sequence_id == sequence.id)
        .order_by(AutomationStep.step_order.asc(), AutomationStep.created_at.asc())
    ).scalars().all()
    return _serialize_sequence(sequence, steps)


# ── Built-in templates (Gemma 4 generated) ────────────────────────────────

BUILTIN_TEMPLATES = [
    {
        "id": "job_completed_send_review",
        "name": "Send Review Request on Job Completion",
        "trigger": "job_completed",
        "action": "send_google_review_request",
        "description": "When a job is marked completed, send a Google review request via SMS.",
        "default_config": {"delay_hours": 2, "channel": "sms"},
    },
    {
        "id": "estimate_approved_create_job",
        "name": "Auto-Create Job on Estimate Approval",
        "trigger": "estimate_approved",
        "action": "create_job",
        "description": "When an estimate is approved, create a job with the estimate line items.",
        "default_config": {"copy_line_items": True, "assign_to_original_tech": True},
    },
    {
        "id": "appointment_reminder_24h",
        "name": "24-Hour Appointment Reminder",
        "trigger": "appointment_scheduled",
        "action": "send_sms_reminder",
        "description": "SMS reminder 24 hours before scheduled appointment.",
        "default_config": {"hours_before": 24, "channel": "sms"},
    },
    {
        "id": "invoice_overdue_30d",
        "name": "30-Day Overdue Invoice Reminder",
        "trigger": "invoice_overdue",
        "action": "send_collection_reminder",
        "description": "Email reminder when invoice is 30+ days past due.",
        "default_config": {"days_threshold": 30, "channel": "email"},
    },
    {
        "id": "new_customer_welcome",
        "name": "Welcome Email on Customer Create",
        "trigger": "customer_created",
        "action": "send_welcome_email",
        "description": "Send branded welcome email when a new customer is created.",
        "default_config": {"include_portal_invite": True},
    },
    {
        "id": "job_scheduled_tech_notify",
        "name": "Notify Tech on Job Assignment",
        "trigger": "job_assigned",
        "action": "send_tech_notification",
        "description": "Push/SMS to technician when a job is assigned to them.",
        "default_config": {"channel": "push", "include_customer_info": True},
    },
]


@router.get("/templates", response_model=None)
def list_automation_templates(
    _: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return {"templates": BUILTIN_TEMPLATES}


@router.post("/{automation_id}/enable", response_model=None)
def enable_automation(
    automation_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    sequence = _get_sequence_or_404(automation_id, db)
    sequence.is_active = True
    db.commit()
    tid, uid = _audit_ids(user, request)
    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="automation_enabled",
        entity_type="automation_sequence", entity_id=str(automation_id),
        details={}, request=request,
    )
    db.commit()
    return {"ok": True, "active": True}


@router.post("/{automation_id}/disable", response_model=None)
def disable_automation(
    automation_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    sequence = _get_sequence_or_404(automation_id, db)
    sequence.is_active = False
    db.commit()
    tid, uid = _audit_ids(user, request)
    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="automation_disabled",
        entity_type="automation_sequence", entity_id=str(automation_id),
        details={}, request=request,
    )
    db.commit()
    return {"ok": True, "active": False}


@router.get("/{automation_id}/history", response_model=None)
def get_automation_run_history(
    automation_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 50,
) -> dict[str, Any]:
    tid, _ = _audit_ids(_, request)
    try:
        rows = db.execute(
            text(
                """SELECT id, action, entity_type, entity_id, details, created_at
                   FROM audit_log
                   WHERE tenant_id = :tid AND entity_id = :aid
                     AND entity_type = 'automation_sequence'
                   ORDER BY created_at DESC LIMIT :limit"""
            ),
            {"tid": tid, "aid": str(automation_id), "limit": limit},
        ).mappings().all()
        return {"items": [dict(r) for r in rows]}
    except Exception:
        log.exception("get_automation_history_failed")
        return {"items": []}


@router.post("/{automation_id}/test", response_model=None)
def test_automation(
    automation_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    sequence = _get_sequence_or_404(automation_id, db)
    steps = db.execute(
        select(AutomationStep)
        .where(AutomationStep.sequence_id == sequence.id)
        .order_by(AutomationStep.step_order.asc())
    ).scalars().all()
    would_do = [
        {
            "step": s.step_order,
            "action_type": s.action_type,
            "delay_seconds": getattr(s, "delay_seconds", 0),
        }
        for s in steps
    ]
    tid, uid = _audit_ids(user, request)
    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="automation_tested",
        entity_type="automation_sequence", entity_id=str(automation_id),
        details={"steps": len(would_do)}, request=request,
    )
    db.commit()
    return {"dry_run": True, "automation_id": str(automation_id), "would_execute": would_do}
