from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import JobTemplate, RecurringJobSchedule
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.job_templates import create_job_from_template

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recurring", tags=["recurring"], dependencies=[Depends(require_module("jobs"))])

ALLOWED_FREQUENCIES = {"weekly", "biweekly", "monthly", "quarterly"}


class RecurringCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    job_template_id: str = Field(min_length=1, max_length=64)
    frequency: str = Field(min_length=1, max_length=20)
    customer_id: UUID
    next_run: datetime


class RecurringPatchIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    frequency: str | None = Field(default=None, min_length=1, max_length=20)
    customer_id: UUID | None = None
    next_run: datetime | None = None
    status: str | None = Field(default=None, max_length=20)


def _tenant_id(request: Request | None) -> str | None:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    tid = str(tenant.get("id") or "").strip()
    return tid or None


def _actor_id(user: dict[str, Any] | None) -> str:
    user = user or {}
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")


def _dt(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else None


def _next_run_from_frequency(base: datetime, frequency: str) -> datetime:
    freq = frequency.lower()
    if freq == "weekly":
        return base + timedelta(days=7)
    if freq == "biweekly":
        return base + timedelta(days=14)
    if freq == "monthly":
        return base + timedelta(days=30)
    if freq == "quarterly":
        return base + timedelta(days=90)
    raise HTTPException(status_code=400, detail="Invalid frequency")


def _serialize_schedule(schedule: RecurringJobSchedule) -> dict[str, Any]:
    return {
        "id": schedule.id,
        "job_template_id": schedule.job_template_id,
        "frequency": schedule.frequency,
        "customer_id": schedule.customer_id,
        "next_run": schedule.next_run,
        "last_run": schedule.last_run,
        "status": schedule.status,
        "created_at": schedule.created_at,
        "updated_at": schedule.updated_at,
    }


def _get_schedule_or_404(schedule_id: str, db: Session) -> RecurringJobSchedule:
    schedule = db.scalars(
        select(RecurringJobSchedule).where(
            RecurringJobSchedule.id == schedule_id,
            RecurringJobSchedule.deleted_at.is_(None),
        )
    ).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Recurring schedule not found")
    return schedule


def materialize_due_recurring_jobs(
    db: Session,
    *,
    now: datetime | None = None,
    actor_id: str = "system",
    tenant_id: str | None = None,
) -> dict[str, int]:
    current = now or datetime.now(UTC)
    current_iso = current.isoformat()

    stmt = (
        select(RecurringJobSchedule)
        .where(
            RecurringJobSchedule.status == "active",
            RecurringJobSchedule.deleted_at.is_(None),
            RecurringJobSchedule.next_run <= current_iso,
        )
        .order_by(RecurringJobSchedule.next_run.asc())
    )
    due_schedules = db.scalars(stmt).all()

    created = 0
    for schedule in due_schedules:
        template = db.scalars(
            select(JobTemplate).where(
                JobTemplate.id == schedule.job_template_id,
                JobTemplate.deleted_at.is_(None),
            )
        ).first()
        if not template:
            continue

        run_at = datetime.fromisoformat(str(schedule.next_run))
        job = create_job_from_template(
            db,
            template=template,
            customer_id=UUID(str(schedule.customer_id)),
            scheduled_at=run_at,
        )

        asyncio.run(log_audit_event(
            db=db,
            tenant_id=tenant_id,
            user_id=actor_id,
            action="recurring_job_materialized",
            entity_type="job",
            entity_id=str(job.id),
            details={"schedule_id": str(schedule.id), "job_template_id": str(schedule.job_template_id)},
        ))

        schedule.last_run = current.isoformat()
        schedule.next_run = _next_run_from_frequency(current, str(schedule.frequency)).isoformat()
        schedule.updated_at = current.isoformat()
        created += 1

    db.commit()
    return {"created_count": created, "due_count": len(due_schedules)}


@router.get("", response_model=None)
def list_recurring_schedules(
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        stmt = (
            select(RecurringJobSchedule)
            .where(RecurringJobSchedule.deleted_at.is_(None))
            .order_by(RecurringJobSchedule.created_at.desc())
        )
        schedules = db.scalars(stmt).all()
        return {"items": [_serialize_schedule(s) for s in schedules]}
    except Exception:
        log.exception("list_recurring_schedules_failed")
        raise HTTPException(status_code=500, detail="Failed to list recurring schedules") from None


@router.post("", response_model=None, status_code=201)
def create_recurring_schedule(
    payload: RecurringCreateIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        if payload.frequency.lower() not in ALLOWED_FREQUENCIES:
            raise HTTPException(status_code=400, detail="Invalid frequency")

        now = datetime.now(UTC).isoformat()
        schedule = RecurringJobSchedule(
            id=str(uuid4()),
            job_template_id=payload.job_template_id,
            frequency=payload.frequency.lower(),
            customer_id=str(payload.customer_id),
            next_run=payload.next_run.isoformat(),
            last_run=None,
            status="active",
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        db.add(schedule)
        asyncio.run(log_audit_event(
            db=db,
            tenant_id=_tenant_id(request),
            user_id=_actor_id(user),
            action="recurring_schedule_created",
            entity_type="recurring_schedule",
            entity_id=schedule.id,
            details={"job_template_id": payload.job_template_id, "frequency": payload.frequency.lower()},
            request=request,
        ))
        db.commit()
        db.refresh(schedule)
        return _serialize_schedule(schedule)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        log.exception("create_recurring_schedule_failed")
        raise HTTPException(status_code=500, detail="Failed to create recurring schedule") from None


@router.patch("/{schedule_id}", response_model=None)
def patch_recurring_schedule(
    schedule_id: str,
    payload: RecurringPatchIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        schedule = _get_schedule_or_404(schedule_id, db)
        updates = payload.model_dump(exclude_unset=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No updatable fields provided")
        if "frequency" in updates and str(updates["frequency"]).lower() not in ALLOWED_FREQUENCIES:
            raise HTTPException(status_code=400, detail="Invalid frequency")

        if "frequency" in updates:
            schedule.frequency = str(updates["frequency"]).lower()
        if "customer_id" in updates:
            schedule.customer_id = str(updates["customer_id"])
        if "next_run" in updates:
            schedule.next_run = _dt(updates["next_run"])
        if "status" in updates:
            schedule.status = str(updates["status"]).lower()

        schedule.updated_at = datetime.now(UTC).isoformat()

        asyncio.run(log_audit_event(
            db=db,
            tenant_id=_tenant_id(request),
            user_id=_actor_id(user),
            action="recurring_schedule_updated",
            entity_type="recurring_schedule",
            entity_id=schedule_id,
            details={"fields": sorted(list(updates.keys()))},
            request=request,
        ))
        db.commit()
        db.refresh(schedule)
        return _serialize_schedule(schedule)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        log.exception("patch_recurring_schedule_failed", extra={"schedule_id": schedule_id})
        raise HTTPException(status_code=500, detail="Failed to update recurring schedule") from None


@router.delete("/{schedule_id}", response_model=None)
def delete_recurring_schedule(
    schedule_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        schedule = _get_schedule_or_404(schedule_id, db)
        now = datetime.now(UTC).isoformat()
        schedule.status = "paused"
        schedule.deleted_at = now
        schedule.updated_at = now

        asyncio.run(log_audit_event(
            db=db,
            tenant_id=_tenant_id(request),
            user_id=_actor_id(user),
            action="recurring_schedule_paused",
            entity_type="recurring_schedule",
            entity_id=schedule_id,
            details={},
            request=request,
        ))
        db.commit()
        return {"ok": True}
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        log.exception("delete_recurring_schedule_failed", extra={"schedule_id": schedule_id})
        raise HTTPException(status_code=500, detail="Failed to cancel recurring schedule") from None
