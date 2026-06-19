"""Service Triggers — auto-generate jobs from service agreements on schedule."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Job, ServiceTrigger
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/service-triggers",
    tags=["service-triggers"],
    dependencies=[Depends(require_module("jobs"))],
)

TRIGGER_STATUSES = ("active", "paused", "completed")


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


class TriggerIn(BaseModel):
    agreement_id: str = Field(min_length=1, max_length=36)
    customer_id: str = Field(min_length=1, max_length=36)
    next_due: str = Field(min_length=10, max_length=30, description="ISO date or datetime")
    interval_months: int = Field(default=12, ge=1, le=120)
    auto_create_job: bool = Field(default=True)


class TriggerPatch(BaseModel):
    next_due: str | None = Field(default=None, max_length=30)
    interval_months: int | None = Field(default=None, ge=1, le=120)
    auto_create_job: bool | None = None
    status: str | None = Field(default=None, max_length=20)


def _serialize(trigger: ServiceTrigger) -> dict[str, Any]:
    return {
        "id": str(trigger.id),
        "company_id": str(trigger.company_id),
        "agreement_id": str(trigger.agreement_id),
        "customer_id": str(trigger.customer_id),
        "next_due": str(trigger.next_due) if trigger.next_due else None,
        "interval_months": int(trigger.interval_months),
        "auto_create_job": bool(trigger.auto_create_job),
        "last_triggered": str(trigger.last_triggered) if trigger.last_triggered else None,
        "status": trigger.status,
        "created_at": str(trigger.created_at) if trigger.created_at else None,
        "updated_at": str(trigger.updated_at) if trigger.updated_at else None,
    }


@router.post("", status_code=201)
def create_trigger(
    request: Request,
    payload: TriggerIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    uid = _uid(user)
    now = datetime.now(timezone.utc)

    # Validate next_due is a parseable date/datetime
    try:
        parsed_next_due = datetime.fromisoformat(payload.next_due.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="next_due must be a valid ISO date or datetime") from None

    trigger = ServiceTrigger(
        id=uuid4(),
        company_id=tid,
        agreement_id=payload.agreement_id,
        customer_id=payload.customer_id,
        next_due=parsed_next_due,
        interval_months=payload.interval_months,
        auto_create_job=payload.auto_create_job,
        status="active",
        created_at=now,
        updated_at=now,
    )

    try:
        db.add(trigger)
        db.commit()
        db.refresh(trigger)
    except Exception:
        db.rollback()
        log.exception("service_trigger_create_failed")
        raise HTTPException(status_code=500, detail="Failed to create service trigger") from None

    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="create",
        entity_type="service_trigger", entity_id=str(trigger.id),
        details={"agreement_id": payload.agreement_id, "customer_id": payload.customer_id,
                 "interval_months": payload.interval_months},
        request=request,
    )
    return _serialize(trigger)


@router.get("")
def list_triggers(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = (
        select(ServiceTrigger)
        .where(
            ServiceTrigger.status == "active",
            ServiceTrigger.deleted_at.is_(None),
        )
        .order_by(ServiceTrigger.next_due.asc())
    )
    triggers = db.scalars(stmt).all()
    return [_serialize(t) for t in triggers]


@router.post("/run")
def run_triggers(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Check all active triggers and create jobs for any that are due."""
    tid = _tid(request)
    uid = _uid(user)
    now = datetime.now(timezone.utc)

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = (
        select(ServiceTrigger)
        .where(
            ServiceTrigger.status == "active",
            ServiceTrigger.deleted_at.is_(None),
            ServiceTrigger.next_due <= now,
        )
    )
    due_triggers = db.scalars(stmt).all()

    jobs_created = []
    triggers_processed = 0

    for trigger in due_triggers:
        triggers_processed += 1
        trigger_id = str(trigger.id)

        if trigger.auto_create_job:
            # Create a maintenance job via ORM
            job = Job(
                id=uuid4(),
                company_id=tid,
                customer_id=trigger.customer_id,
                title=f"Scheduled maintenance (agreement {trigger.agreement_id})",
                description=f"Scheduled maintenance (agreement {trigger.agreement_id})",
                lifecycle_stage="scheduled",
                dispatch_status="unassigned",
                billing_status="unbilled",
                source="service_trigger",
                created_at=now,
            )
            try:
                db.add(job)
                jobs_created.append({
                    "job_id": str(job.id),
                    "trigger_id": trigger_id,
                    "customer_id": str(trigger.customer_id),
                })
            except Exception:
                log.exception("service_trigger_job_create_failed for trigger %s", trigger_id)
                continue

        # Advance next_due by interval_months
        interval = int(trigger.interval_months)
        next_month = now.month + interval
        next_year = now.year + (next_month - 1) // 12
        next_month = ((next_month - 1) % 12) + 1
        next_day = min(now.day, 28)  # safe day for all months
        next_due = datetime(next_year, next_month, next_day, tzinfo=timezone.utc)

        trigger.last_triggered = now
        trigger.next_due = next_due
        trigger.updated_at = now

    try:
        db.commit()
    except Exception:
        db.rollback()
        log.exception("service_triggers_run_commit_failed")
        raise HTTPException(status_code=500, detail="Failed to process service triggers") from None

    if jobs_created:
        log_audit_event_sync(
            db, tenant_id=tid, user_id=uid, action="create",
            entity_type="service_trigger_run", entity_id=str(uuid4()),
            details={"triggers_processed": triggers_processed, "jobs_created": len(jobs_created)},
            request=request,
        )

    return {
        "triggers_processed": triggers_processed,
        "jobs_created": jobs_created,
    }


@router.patch("/{trigger_id}")
def update_trigger(
    trigger_id: str,
    request: Request,
    payload: TriggerPatch,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    uid = _uid(user)

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    trigger = db.scalars(
        select(ServiceTrigger).where(
            ServiceTrigger.id == trigger_id,
            ServiceTrigger.deleted_at.is_(None),
        )
    ).first()
    if not trigger:
        raise HTTPException(status_code=404, detail="Service trigger not found")

    now = datetime.now(timezone.utc)

    if payload.next_due is not None:
        try:
            parsed = datetime.fromisoformat(payload.next_due.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            raise HTTPException(status_code=422, detail="next_due must be a valid ISO date or datetime") from None
        trigger.next_due = parsed
    if payload.interval_months is not None:
        trigger.interval_months = payload.interval_months
    if payload.auto_create_job is not None:
        trigger.auto_create_job = payload.auto_create_job
    if payload.status is not None:
        if payload.status not in TRIGGER_STATUSES:
            raise HTTPException(status_code=422, detail=f"Invalid status. Must be one of: {', '.join(TRIGGER_STATUSES)}")
        trigger.status = payload.status

    trigger.updated_at = now

    try:
        db.commit()
        db.refresh(trigger)
    except Exception:
        db.rollback()
        log.exception("service_trigger_update_failed")
        raise HTTPException(status_code=500, detail="Failed to update service trigger") from None

    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="update",
        entity_type="service_trigger", entity_id=trigger_id,
        details={"changes": payload.model_dump(exclude_none=True)},
        request=request,
    )
    return _serialize(trigger)
