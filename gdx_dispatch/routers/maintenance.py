"""
Maintenance router — recurring maintenance plans & customer enrollments.

Customers enroll in plans (e.g., "Annual Garage Door Tune-Up, 2 visits/year").
Enrollments track next_service_date and auto-reschedule when advanced.

Pattern mirrors gdx_dispatch/routers/proposals.py (tenant-scoped CRUD + audit).
"""
from __future__ import annotations

import calendar
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import (
    select,
)
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["maintenance"],
    dependencies=[Depends(require_module("jobs")), Depends(require_role("admin", "owner", "superadmin"))],
)


BILLING_TYPES = ("monthly", "annual", "per_visit")
ENROLLMENT_STATUSES = ("active", "paused", "cancelled")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
from gdx_dispatch.models.tenant_models import MaintenancePlan, PlanEnrollment  # noqa: E402


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------
class PlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    visits_per_year: int = Field(default=1, ge=1, le=52)
    billing_type: str = Field(default="annual", pattern=r"^(monthly|annual|per_visit)$")
    price: float = Field(default=0, ge=0, le=1_000_000)
    active: bool = True


class PlanPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    visits_per_year: int | None = Field(default=None, ge=1, le=52)
    billing_type: str | None = Field(
        default=None, pattern=r"^(monthly|annual|per_visit)$"
    )
    price: float | None = Field(default=None, ge=0, le=1_000_000)
    active: bool | None = None


class EnrollmentIn(BaseModel):
    plan_id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    start_date: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)


class EnrollmentPatch(BaseModel):
    status: str | None = Field(default=None, pattern=r"^(active|paused|cancelled)$")
    notes: str | None = Field(default=None, max_length=2000)


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


def _add_months(base: datetime, months: int) -> datetime:
    """Add whole months to a datetime without pulling in dateutil.

    Clamps day-of-month to the last valid day of the target month
    (e.g., Jan 31 + 1 month -> Feb 28/29).
    """
    if months <= 0:
        return base
    total = base.month - 1 + months
    new_year = base.year + total // 12
    new_month = (total % 12) + 1
    last_day = calendar.monthrange(new_year, new_month)[1]
    new_day = min(base.day, last_day)
    return base.replace(year=new_year, month=new_month, day=new_day)


def _months_between_visits(visits_per_year: int) -> int:
    """Months between visits given visits_per_year (floor, min 1)."""
    if visits_per_year <= 0:
        return 12
    return max(1, 12 // visits_per_year)


def _serialize_plan(p: MaintenancePlan) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "company_id": p.company_id,
        "name": p.name,
        "description": p.description,
        "visits_per_year": p.visits_per_year,
        "billing_type": p.billing_type,
        "price": float(p.price or 0),
        "active": bool(p.active),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _serialize_enrollment(e: PlanEnrollment) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "company_id": e.company_id,
        "plan_id": str(e.plan_id),
        "customer_id": str(e.customer_id),
        "status": e.status,
        "start_date": e.start_date.isoformat() if e.start_date else None,
        "next_service_date": e.next_service_date.isoformat() if e.next_service_date else None,
        "visits_completed": e.visits_completed,
        "notes": e.notes,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "updated_at": e.updated_at.isoformat() if e.updated_at else None,
    }


def _get_plan_scoped(db: Session, plan_id: UUID, tenant_id: str) -> MaintenancePlan:
    row = db.execute(
        select(MaintenancePlan).where(
            MaintenancePlan.id == plan_id,
            MaintenancePlan.company_id == tenant_id,
            MaintenancePlan.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Maintenance plan not found")
    return row


def _get_enrollment_scoped(
    db: Session, enrollment_id: UUID, tenant_id: str
) -> PlanEnrollment:
    row = db.execute(
        select(PlanEnrollment).where(
            PlanEnrollment.id == enrollment_id,
            PlanEnrollment.company_id == tenant_id,
            PlanEnrollment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    return row


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
            "maintenance_audit_failed action=%s entity_type=%s entity_id=%s",
            action,
            entity_type,
            entity_id,
        )
        db.rollback()


# ---------------------------------------------------------------------------
# Plan endpoints
# ---------------------------------------------------------------------------
@router.get("/api/maintenance/plans", response_model=None)
def list_plans(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    active_only: bool = True,
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = select(MaintenancePlan).where(
        MaintenancePlan.deleted_at.is_(None),
    )
    if active_only:
        stmt = stmt.where(MaintenancePlan.active.is_(True))
    stmt = stmt.order_by(MaintenancePlan.created_at.desc())
    rows = db.execute(stmt).scalars().all()
    return [_serialize_plan(r) for r in rows]


@router.post("/api/maintenance/plans", response_model=None, status_code=201)
def create_plan(
    payload: PlanIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    p = MaintenancePlan(
        company_id=tenant_id,
        name=payload.name.strip(),
        description=payload.description,
        visits_per_year=payload.visits_per_year,
        billing_type=payload.billing_type,
        price=Decimal(str(payload.price)),
        active=payload.active,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="maintenance_plan_created",
        entity_type="maintenance_plan",
        entity_id=str(p.id),
        details={"name": p.name, "visits_per_year": p.visits_per_year},
        request=request,
    )
    return _serialize_plan(p)


@router.patch("/api/maintenance/plans/{plan_id}", response_model=None)
def update_plan(
    plan_id: UUID,
    payload: PlanPatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    p = _get_plan_scoped(db, plan_id, tenant_id)
    data = payload.model_dump(exclude_unset=True)
    for field in ("name", "description", "billing_type", "visits_per_year", "active"):
        if field in data and data[field] is not None:
            setattr(p, field, data[field])
    if "price" in data and data["price"] is not None:
        p.price = Decimal(str(data["price"]))
    db.commit()
    db.refresh(p)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="maintenance_plan_updated",
        entity_type="maintenance_plan",
        entity_id=str(p.id),
        details={"fields": list(data.keys())},
        request=request,
    )
    return _serialize_plan(p)


@router.delete("/api/maintenance/plans/{plan_id}", response_model=None, status_code=204)
def delete_plan(
    plan_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    p = _get_plan_scoped(db, plan_id, tenant_id)
    p.deleted_at = utcnow()
    db.commit()
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="maintenance_plan_deleted",
        entity_type="maintenance_plan",
        entity_id=str(plan_id),
        request=request,
    )
    return None


# ---------------------------------------------------------------------------
# Enrollment endpoints
# ---------------------------------------------------------------------------
@router.get("/api/maintenance/enrollments", response_model=None)
def list_enrollments(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    status: str | None = None,
    customer_id: str | None = None,
    plan_id: str | None = None,
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = select(PlanEnrollment).where(
        PlanEnrollment.deleted_at.is_(None),
    )
    if status:
        stmt = stmt.where(PlanEnrollment.status == status)
    if customer_id:
        try:
            stmt = stmt.where(PlanEnrollment.customer_id == UUID(customer_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid customer_id") from None
    if plan_id:
        try:
            stmt = stmt.where(PlanEnrollment.plan_id == UUID(plan_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid plan_id") from None
    stmt = stmt.order_by(PlanEnrollment.created_at.desc())
    rows = db.execute(stmt).scalars().all()
    return [_serialize_enrollment(r) for r in rows]


@router.post("/api/maintenance/enrollments", response_model=None, status_code=201)
def create_enrollment(
    payload: EnrollmentIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    try:
        plan_uuid = UUID(payload.plan_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid plan_id") from None
    try:
        customer_uuid = UUID(payload.customer_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid customer_id") from None

    # Verify plan exists and belongs to this tenant
    plan = _get_plan_scoped(db, plan_uuid, tenant_id)

    start = payload.start_date or utcnow()
    # Ensure timezone-aware
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    months_gap = _months_between_visits(plan.visits_per_year)
    next_service = _add_months(start, months_gap)

    e = PlanEnrollment(
        company_id=tenant_id,
        plan_id=plan_uuid,
        customer_id=customer_uuid,
        status="active",
        start_date=start,
        next_service_date=next_service,
        visits_completed=0,
        notes=payload.notes,
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="enrollment_created",
        entity_type="plan_enrollment",
        entity_id=str(e.id),
        details={"plan_id": str(plan_uuid), "customer_id": str(customer_uuid)},
        request=request,
    )
    return _serialize_enrollment(e)


@router.patch("/api/maintenance/enrollments/{enrollment_id}", response_model=None)
def update_enrollment(
    enrollment_id: UUID,
    payload: EnrollmentPatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    e = _get_enrollment_scoped(db, enrollment_id, tenant_id)
    data = payload.model_dump(exclude_unset=True)
    if "status" in data and data["status"] is not None:
        e.status = data["status"]
    if "notes" in data:
        e.notes = data["notes"]
    db.commit()
    db.refresh(e)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="enrollment_updated",
        entity_type="plan_enrollment",
        entity_id=str(e.id),
        details={"fields": list(data.keys())},
        request=request,
    )
    return _serialize_enrollment(e)


@router.post(
    "/api/maintenance/enrollments/{enrollment_id}/advance", response_model=None
)
def advance_enrollment(
    enrollment_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    e = _get_enrollment_scoped(db, enrollment_id, tenant_id)
    if e.status != "active":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot advance enrollment in status '{e.status}'",
        )
    plan = _get_plan_scoped(db, e.plan_id, tenant_id)
    months_gap = _months_between_visits(plan.visits_per_year)

    e.visits_completed = (e.visits_completed or 0) + 1
    base = e.next_service_date or utcnow()
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    e.next_service_date = _add_months(base, months_gap)
    db.commit()
    db.refresh(e)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="enrollment_advanced",
        entity_type="plan_enrollment",
        entity_id=str(e.id),
        details={
            "visits_completed": e.visits_completed,
            "next_service_date": e.next_service_date.isoformat()
            if e.next_service_date
            else None,
        },
        request=request,
    )
    return _serialize_enrollment(e)


@router.get("/api/maintenance/due-this-month", response_model=None)
def due_this_month(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    now = utcnow()
    month_start = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    last_day = calendar.monthrange(now.year, now.month)[1]
    # Exclusive upper bound: first of next month
    if now.month == 12:
        month_end_exclusive = month_start.replace(year=now.year + 1, month=1)
    else:
        month_end_exclusive = month_start.replace(month=now.month + 1)
    _ = last_day  # kept for clarity
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = (
        select(PlanEnrollment)
        .where(
            PlanEnrollment.deleted_at.is_(None),
            PlanEnrollment.status == "active",
            PlanEnrollment.next_service_date.is_not(None),
            PlanEnrollment.next_service_date >= month_start,
            PlanEnrollment.next_service_date < month_end_exclusive,
        )
        .order_by(PlanEnrollment.next_service_date.asc())
    )
    rows = db.execute(stmt).scalars().all()
    return [_serialize_enrollment(r) for r in rows]
