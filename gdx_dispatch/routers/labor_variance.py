"""S97 slice 8 — labor variance per job.

`GET /api/jobs/{job_id}/labor-variance` compares estimated labor (sum of
EstimateLine.estimated_man_hours on the accepted estimate, valued at the
truthiest available hourly rate) against actual labor (per-tech wall-clock
hours from JobAssignment.arrived_at → completed_at, valued the same way).

Rate hierarchy per CLAUDE.md / labor_pricing.py docstring:

    1. PayrollEntry — gross_pay / hours_paid for the entry whose
       (period_start, period_end) covers the job's work date. This is the
       *truth* — what the company actually paid.
    2. Technician.hourly_rate — the *estimated* rate fallback.
    3. User.hourly_rate — last resort.

The `rate_source` field on the response captures which level was used per
tech, so reviewers can spot whether a variance is "we underestimated" or
"we don't have payroll loaded yet."
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.models.tenant_models import Job, JobAssignment, PayrollEntry, Technician, User
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(
    prefix="/api",
    tags=["labor-variance"],
    dependencies=[Depends(require_module("jobs"))],
)


def _resolve_rate(
    db: Session,
    tech_id: str | None,
    user_id: str | None,
    work_date: datetime | None,
) -> tuple[Decimal | None, str]:
    """Returns (rate, source) where source ∈ {'payroll','technician','user','none'}."""

    payroll_user_id = user_id
    if payroll_user_id is None and tech_id is not None:
        tech_row = db.execute(
            select(Technician).where(Technician.id == tech_id)
        ).scalar_one_or_none()
        if tech_row is not None:
            payroll_user_id = tech_row.user_id

    if payroll_user_id and work_date is not None:
        entry = db.execute(
            select(PayrollEntry)
            .where(
                PayrollEntry.tech_user_id == payroll_user_id,
                PayrollEntry.deleted_at.is_(None),
                PayrollEntry.period_start <= work_date,
                PayrollEntry.period_end >= work_date,
            )
            .order_by(PayrollEntry.period_end.desc())
            .limit(1)
        ).scalar_one_or_none()
        if entry is not None and entry.hours_paid and Decimal(entry.hours_paid) > 0:
            return (
                (Decimal(entry.gross_pay) / Decimal(entry.hours_paid)).quantize(Decimal("0.01")),
                "payroll",
            )

    if tech_id is not None:
        tech_row = db.execute(
            select(Technician).where(Technician.id == tech_id)
        ).scalar_one_or_none()
        if tech_row is not None and tech_row.hourly_rate is not None:
            return (Decimal(tech_row.hourly_rate), "technician")

    if payroll_user_id:
        try:
            user_uuid = payroll_user_id if isinstance(payroll_user_id, UUID) else UUID(str(payroll_user_id))
        except (TypeError, ValueError):
            user_uuid = None
        if user_uuid is not None:
            user_row = db.execute(
                select(User).where(User.id == user_uuid)
            ).scalar_one_or_none()
            if user_row is not None and user_row.hourly_rate is not None:
                return (Decimal(str(user_row.hourly_rate)), "user")

    return (None, "none")


@router.get(
    "/jobs/{job_id}/labor-variance",
    dependencies=[Depends(require_permission("jobs.read_all"))],
)
def labor_variance(
    job_id: UUID,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    job = db.execute(
        select(Job).where(Job.id == job_id, Job.deleted_at.is_(None))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    work_date = (
        getattr(job, "completed_at", None)
        or getattr(job, "started_at", None)
        or getattr(job, "scheduled_at", None)
        or getattr(job, "created_at", None)
        or datetime.now(timezone.utc)
    )

    # --- estimated side ---
    est = db.execute(
        select(Estimate)
        .where(
            Estimate.job_id == job_id,
            Estimate.deleted_at.is_(None),
            Estimate.status == "accepted",
        )
        .order_by(Estimate.accepted_at.desc().nullslast())
        .limit(1)
    ).scalar_one_or_none()
    if est is None:
        est = db.execute(
            select(Estimate)
            .where(
                Estimate.job_id == job_id,
                Estimate.deleted_at.is_(None),
                Estimate.status.in_(("draft", "sent")),
            )
            .order_by(Estimate.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    estimated_hours = Decimal("0")
    if est is not None:
        # Doug 2026-05-07 / EST-000030: hours are per-row from the matrix,
        # quantity multiplies. Pre-fix sum() ignored qty and a 4-door
        # commercial job's variance baselined off 1-door's worth of time.
        rows = db.execute(
            select(EstimateLine.estimated_man_hours, EstimateLine.quantity).where(
                EstimateLine.estimate_id == est.id,
                EstimateLine.estimated_man_hours.is_not(None),
            )
        ).all()
        for hours, qty in rows:
            if hours is None:
                continue
            estimated_hours += Decimal(str(hours)) * Decimal(int(qty or 1))

    # --- actual side ---
    assignments = db.execute(
        select(JobAssignment).where(
            JobAssignment.job_id == str(job_id),
            JobAssignment.deleted_at.is_(None),
        )
    ).scalars().all()

    per_tech: list[dict[str, Any]] = []
    actual_hours = Decimal("0")
    actual_cost = Decimal("0")
    estimated_cost = Decimal("0")

    # Estimated cost uses the *primary* tech's rate (lead, else first-assigned).
    primary = next((a for a in assignments if a.is_lead), assignments[0] if assignments else None)
    primary_rate, primary_source = _resolve_rate(
        db,
        tech_id=primary.tech_id if primary else None,
        user_id=primary.user_id if primary else None,
        work_date=work_date,
    )
    if primary_rate is not None and estimated_hours > 0:
        estimated_cost = (Decimal(estimated_hours) * primary_rate).quantize(Decimal("0.01"))

    for a in assignments:
        # Wall-clock per tech: arrived_at → completed_at (clamped to >= 0).
        a_hours = Decimal("0")
        if a.arrived_at and a.completed_at:
            seconds = (a.completed_at - a.arrived_at).total_seconds()
            a_hours = (Decimal(str(max(seconds, 0))) / Decimal("3600")).quantize(Decimal("0.01"))
        rate, source = _resolve_rate(
            db, tech_id=a.tech_id, user_id=a.user_id, work_date=work_date,
        )
        a_cost = (a_hours * rate).quantize(Decimal("0.01")) if (rate is not None and a_hours > 0) else Decimal("0")
        per_tech.append({
            "assignment_id": a.id,
            "tech_id": a.tech_id,
            "user_id": a.user_id,
            "is_lead": bool(a.is_lead),
            "arrived_at": a.arrived_at.isoformat() if a.arrived_at else None,
            "completed_at": a.completed_at.isoformat() if a.completed_at else None,
            "actual_hours": float(a_hours),
            "hourly_rate": float(rate) if rate is not None else None,
            "rate_source": source,
            "actual_cost": float(a_cost),
        })
        actual_hours += a_hours
        actual_cost += a_cost

    variance_hours = actual_hours - estimated_hours
    variance_cost = actual_cost - estimated_cost

    return {
        "job_id": str(job_id),
        "estimate_id": str(est.id) if est is not None else None,
        "estimate_status": est.status if est is not None else None,
        "work_date": work_date.isoformat() if work_date else None,
        "estimated_hours": float(estimated_hours),
        "actual_hours": float(actual_hours),
        "variance_hours": float(variance_hours),
        "estimated_cost": float(estimated_cost),
        "actual_cost": float(actual_cost),
        "variance_cost": float(variance_cost),
        "primary_rate": float(primary_rate) if primary_rate is not None else None,
        "primary_rate_source": primary_source,
        "per_tech": per_tech,
    }
