"""
Appointments router — dispatch calendar/appointment layer.

Distinct from gdx_dispatch/routers/booking.py (customer booking intake). An appointment is a
scheduled time block tied to a technician, optionally linked to a job and
customer. Supports confirmation workflow, "on my way" status transitions,
arrived/completed/cancelled lifecycle, and per-day mapping queries.

Pattern mirrors gdx_dispatch/routers/change_orders.py
(inline model + CRUD + state transitions + audit). Gated behind the "jobs"
module.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.core.quantities import recorded_quantity
from gdx_dispatch.core.roles import DISPATCH_MANAGER_ROLES
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["appointments"],
    dependencies=[Depends(require_module("jobs"))],
)


APPOINTMENT_STATUSES = (
    "scheduled",
    "confirmed",
    "en_route",
    "arrived",
    "completed",
    "cancelled",
)


from gdx_dispatch.models.tenant_models import Appointment, JobAssignment  # noqa: E402
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine  # noqa: E402


# S97 slice 7 — scheduler block sizing from labor matrix man-hours.
# Wall-clock duration = sum(estimated_man_hours × quantity on accepted estimate)
#                       / max(crew_size, 1)
# rounded UP to the next 15-minute increment, floored at the per-row
# min_wall_clock_minutes from the labor matrix.
# Returns None when there's no accepted estimate, no estimate lines with
# man-hours, or job_id unknown.
#
# Doug 2026-05-07 / EST-000030 retro: pre-fix this function summed hours
# without quantity (a 4-door install at qty=4 scheduled 1 door's worth of
# time) and used JobAssignment count as crew_size unconditionally (which is
# 0 before staffing). Now: hours×qty is the contract, crew_size honors the
# matrix row's default_crew_size when JobAssignment is empty, and the
# per-row min_wall_clock_minutes prevents 8-tech-on-1-door 7-min absurdity.
def compute_man_hour_duration_minutes(db: Session, job_id: Any) -> int | None:
    from gdx_dispatch.models.labor_pricing import LaborPriceItem

    if job_id is None:
        return None
    try:
        job_uuid = job_id if isinstance(job_id, UUID) else UUID(str(job_id))
    except (TypeError, ValueError):
        return None

    # Prefer the accepted estimate; fall back to the most recently updated
    # non-declined estimate so duration suggestions still work pre-acceptance.
    est = db.execute(
        select(Estimate)
        .where(
            Estimate.job_id == job_uuid,
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
                Estimate.job_id == job_uuid,
                Estimate.deleted_at.is_(None),
                Estimate.status.in_(("draft", "sent")),
            )
            .order_by(Estimate.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    if est is None:
        return None

    rows = db.execute(
        select(
            EstimateLine.estimated_man_hours,
            EstimateLine.quantity,
            EstimateLine.labor_price_item_id,
        ).where(
            EstimateLine.estimate_id == est.id,
            EstimateLine.estimated_man_hours.is_not(None),
        )
    ).all()

    # Aggregate qty-aware man-hours and the max default_crew_size /
    # min_wall_clock across labor-matrix rows on this estimate.
    total_hours = Decimal("0")
    matrix_crew_max = 0
    floor_minutes = 0
    for hours, qty, item_id in rows:
        if hours is None:
            continue
        total_hours += Decimal(str(hours)) * Decimal(int(recorded_quantity(qty)))
        if item_id is not None:
            row = db.get(LaborPriceItem, item_id)
            if row is not None:
                matrix_crew_max = max(matrix_crew_max, int(row.default_crew_size or 1))
                floor_minutes = max(floor_minutes, int(row.min_wall_clock_minutes or 0))

    if total_hours <= 0:
        return None

    assigned = db.execute(
        select(JobAssignment).where(
            JobAssignment.job_id == str(job_uuid),
            JobAssignment.deleted_at.is_(None),
        )
    ).scalars().all()
    # Prefer JobAssignment count when staffed (operator decided), else fall
    # back to matrix recommendation, else 1.
    crew_size = max(len(assigned), matrix_crew_max, 1)

    minutes = float(total_hours) * 60.0 / crew_size
    # Round UP to the next 15-min increment.
    rounded = int(((minutes + 14.999) // 15) * 15)
    return max(15, rounded, floor_minutes)


class AppointmentIn(BaseModel):
    job_id: str | None = Field(default=None, max_length=64)
    customer_id: str | None = Field(default=None, max_length=64)
    tech_id: str | None = Field(default=None, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    address: str | None = Field(default=None, max_length=500)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    start_at: datetime
    end_at: datetime
    notes: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def _validate_range(self) -> AppointmentIn:
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class AppointmentPatch(BaseModel):
    tech_id: str | None = Field(default=None, max_length=64)
    # Tier-6 (2026-07): the edit dialog has always sent job/customer relinks;
    # the PATCH silently dropped both.
    job_id: str | None = Field(default=None, max_length=64)
    customer_id: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    address: str | None = Field(default=None, max_length=500)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    start_at: datetime | None = None
    end_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=5000)
    status: str | None = Field(
        default=None,
        pattern=r"^(scheduled|confirmed|en_route|arrived|completed|cancelled)$",
    )
    # Arrival is always recorded (multi-day jobs plan §5.2a): a time can be
    # entered or corrected here, never cleared (that is Undo arrival).
    arrived_at: datetime | None = None


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class ArrivedIn(BaseModel):
    """Office Arrived: when the crew got there; defaults to now."""

    arrived_at: datetime | None = None


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


def _parse_uuid(raw: str | None, field: str) -> UUID | None:
    if raw is None or raw == "":
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail=f"Invalid {field}") from None


def _serialize(a: Appointment) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "company_id": a.company_id,
        "job_id": str(a.job_id) if a.job_id else None,
        "customer_id": str(a.customer_id) if a.customer_id else None,
        "tech_id": a.tech_id,
        "title": a.title,
        "description": a.description,
        "address": a.address,
        "lat": float(a.lat) if a.lat is not None else None,
        "lng": float(a.lng) if a.lng is not None else None,
        "start_at": a.start_at.isoformat() if a.start_at else None,
        "end_at": a.end_at.isoformat() if a.end_at else None,
        "status": a.status,
        "confirmed_at": a.confirmed_at.isoformat() if a.confirmed_at else None,
        "en_route_at": a.en_route_at.isoformat() if a.en_route_at else None,
        "arrived_at": a.arrived_at.isoformat() if a.arrived_at else None,
        "completed_at": a.completed_at.isoformat() if a.completed_at else None,
        "notes": a.notes,
        "created_by": a.created_by,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


def _get_scoped(db: Session, appt_id: UUID, tenant_id: str) -> Appointment:
    row = db.execute(
        select(Appointment).where(
            Appointment.id == appt_id,
            Appointment.company_id == tenant_id,
            Appointment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return row


def _record(
    db: Session,
    a: Appointment,
    *,
    tenant_id: str,
    user: Any,
    action: str,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
    job_ids: tuple = (),
) -> None:
    """Audit, keep each touched job's date on its earliest Current visit
    (invariant I, multi-day jobs plan §5.2a), and commit once: the change,
    its audit row and the recompute land together or not at all."""
    from gdx_dispatch.models.tenant_models import Job  # noqa: PLC0415
    from gdx_dispatch.services.visit_sync import recompute_job_schedule  # noqa: PLC0415

    db.flush()
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=_user_id(user),
        action=action,
        entity_type="appointment",
        entity_id=str(a.id),
        details=details or {},
        request=request,
    )
    for job_id in dict.fromkeys(j for j in job_ids if j is not None):
        job = db.get(Job, job_id)
        if job is not None and job.deleted_at is None:
            recompute_job_schedule(db, job, _user_id(user), action)
    db.commit()
    db.refresh(a)


def _conflict(message: str, code: str, **detail: Any) -> JSONResponse:
    return JSONResponse({"detail": message, "code": code, **detail}, status_code=409)


def _status_only_arrival(a: Appointment) -> bool:
    """An old visit marked "arrived" with no time (written before arrivals
    were always recorded)."""
    return a.job_id is not None and a.arrived_at is None and (a.status or "") == "arrived"


def _ask_about_status_only(a: Appointment) -> JSONResponse:
    return _conflict(
        "This visit is marked arrived with no time recorded. Enter its arrival "
        "time or undo the arrival first, so the record is not lost.",
        "needs_answer",
        question="status_only_arrival",
        visit_ids=[str(a.id)],
    )


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands a timestamp back naive; it was stored as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_iso_date(raw: str, field: str) -> datetime:
    try:
        # Accept full ISO datetime or bare date.
        if "T" in raw or " " in raw:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {field} (ISO format required)") from None


@router.get("/api/appointments", response_model=None)
def list_appointments(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    start: str | None = None,
    end: str | None = None,
    tech_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))

    # When neither start nor end is supplied, default to a permissive
    # window centered on now (past 30 days .. next 90 days). The previous
    # default of "today UTC only" silently hid every appointment scheduled
    # in the user's local "today" once UTC midnight rolled over — at 8pm
    # EDT all of "today's" jobs vanished from the calendar (Doug, S109).
    now = datetime.now(timezone.utc)
    start_dt = _parse_iso_date(start, "start") if start else now - timedelta(days=30)
    if end:
        end_dt = _parse_iso_date(end, "end")
    elif start:
        # Caller pinned start but not end — keep the legacy 1-day window so
        # explicit single-day queries (e.g., the calendar's day view) still
        # return just that day.
        end_dt = datetime.combine(
            start_dt.date() + timedelta(days=1), time.min, tzinfo=timezone.utc
        )
    else:
        end_dt = now + timedelta(days=90)

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = select(Appointment).where(
        Appointment.deleted_at.is_(None),
        Appointment.start_at >= start_dt,
        Appointment.start_at < end_dt,
    )
    if tech_id:
        stmt = stmt.where(Appointment.tech_id == tech_id)
    if status:
        stmt = stmt.where(Appointment.status == status)
    stmt = stmt.order_by(Appointment.start_at.asc()).limit(limit).offset(offset)
    rows = db.execute(stmt).scalars().all()
    return [_serialize(r) for r in rows]


@router.get("/api/jobs/{job_id}/suggested-duration", response_model=None)
def get_suggested_duration(
    job_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """S97 slice 7 — UI hint: how long should this job's appointment block be?

    Returns the man-hour math the create-appointment handler would apply.
    `duration_minutes` is None when there's no estimate or no man-hours; the
    UI should fall back to the operator's default (60 min) in that case.
    """
    rows = db.execute(
        select(JobAssignment).where(
            JobAssignment.job_id == str(job_id),
            JobAssignment.deleted_at.is_(None),
        )
    ).scalars().all()
    crew_size = max(len(rows), 1)

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

    total_hours: Decimal = Decimal("0")
    if est is not None:
        line_rows = db.execute(
            select(EstimateLine.estimated_man_hours, EstimateLine.quantity).where(
                EstimateLine.estimate_id == est.id,
                EstimateLine.estimated_man_hours.is_not(None),
            )
        ).all()
        # qty-aware sum, matches compute_man_hour_duration_minutes contract.
        for hours, qty in line_rows:
            if hours is None:
                continue
            total_hours += Decimal(str(hours)) * Decimal(int(recorded_quantity(qty)))

    minutes = compute_man_hour_duration_minutes(db, job_id)
    return {
        "job_id": str(job_id),
        "estimate_id": str(est.id) if est is not None else None,
        "estimate_status": est.status if est is not None else None,
        "man_hours": float(total_hours) if total_hours > 0 else None,
        "crew_size": crew_size,
        "duration_minutes": minutes,
    }


@router.post("/api/appointments", response_model=None, status_code=201)
def create_appointment(
    payload: AppointmentIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    job_uuid = _parse_uuid(payload.job_id, "job_id")
    customer_uuid = _parse_uuid(payload.customer_id, "customer_id")

    # S97 slice 7 — auto-size the appointment block from labor matrix man-hours.
    # If the caller supplied the implicit 60-minute default end_at, prefer the
    # man-hour math (sum estimated_man_hours / crew_size, rounded up to 15min).
    # An explicitly-set duration (anything other than exactly 60 min) is kept
    # as-is — operators can always override the suggestion.
    suggested = compute_man_hour_duration_minutes(db, job_uuid)
    requested_minutes = int(round((payload.end_at - payload.start_at).total_seconds() / 60))
    final_start = payload.start_at
    final_end = payload.end_at
    final_duration = requested_minutes
    if suggested and requested_minutes == 60:
        final_end = payload.start_at + timedelta(minutes=suggested)
        final_duration = suggested

    a = Appointment(
        company_id=tenant_id,
        job_id=job_uuid,
        customer_id=customer_uuid,
        tech_id=payload.tech_id,
        title=payload.title.strip(),
        description=payload.description,
        address=payload.address,
        lat=Decimal(str(payload.lat)) if payload.lat is not None else None,
        lng=Decimal(str(payload.lng)) if payload.lng is not None else None,
        start_at=final_start,
        end_at=final_end,
        duration_minutes=final_duration,
        status="scheduled",
        notes=payload.notes,
        created_by=_user_id(user),
    )
    db.add(a)
    _record(
        db, a, tenant_id=tenant_id, user=user, action="appointment_created",
        details={"title": a.title, "tech_id": a.tech_id},
        request=request, job_ids=(a.job_id,),
    )
    return _serialize(a)


@router.get("/api/appointments/unconfirmed", response_model=None)
def list_unconfirmed(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    hours: int = 48,
) -> list[dict[str, Any]]:
    hours = max(1, min(int(hours or 48), 24 * 30))
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours)
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = (
        select(Appointment)
        .where(
            Appointment.deleted_at.is_(None),
            Appointment.status == "scheduled",
            Appointment.start_at >= now,
            Appointment.start_at <= horizon,
        )
        .order_by(Appointment.start_at.asc())
    )
    rows = db.execute(stmt).scalars().all()
    return [_serialize(r) for r in rows]


@router.get("/api/appointments/{appt_id}", response_model=None)
def get_appointment(
    appt_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    return _serialize(_get_scoped(db, appt_id, tenant_id))


@router.patch("/api/appointments/{appt_id}", response_model=None)
def update_appointment(
    appt_id: UUID,
    payload: AppointmentPatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    a = _get_scoped(db, appt_id, tenant_id)
    data = payload.model_dump(exclude_unset=True)

    new_start = data.get("start_at", a.start_at)
    new_end = data.get("end_at", a.end_at)
    if new_start and new_end and new_end <= new_start:
        raise HTTPException(status_code=422, detail="end_at must be after start_at")

    # Arrival is always recorded (multi-day jobs plan §5.2a).
    if "arrived_at" in data and data["arrived_at"] is None and (
        a.arrived_at is not None or data.get("status") == "arrived"
    ):
        raise HTTPException(
            status_code=422,
            detail="An arrival time cannot be cleared here. Use Undo arrival.",
        )
    old_job_id = a.job_id
    if "job_id" in data:
        new_job_id = _parse_uuid(data["job_id"], "job_id")
        if a.arrived_at is not None and old_job_id is not None and new_job_id != old_job_id:
            return _conflict(
                "This visit has an arrival recorded, so it cannot leave its job. "
                "Use Undo arrival for a mis-tap, or Cancel for a duplicate the crew worked.",
                "visit_arrived",
                visit_id=str(a.id),
            )
    if (
        data.get("status") == "cancelled"
        and a.status != "cancelled"
        and _status_only_arrival(a)
    ):
        return _ask_about_status_only(a)

    arrival_change = None
    if data.get("arrived_at") is not None:
        if data["arrived_at"].tzinfo is None:
            data["arrived_at"] = data["arrived_at"].replace(tzinfo=timezone.utc)
        if data["arrived_at"] > utcnow() + timedelta(minutes=5):
            raise HTTPException(status_code=422, detail="arrived_at is in the future")
        if _aware(a.arrived_at) != data["arrived_at"]:
            arrival_change = {
                "old": _iso(a.arrived_at), "new": _iso(data["arrived_at"]), "source": "manual",
            }
            a.arrived_at = data["arrived_at"]
    elif (
        data.get("status") == "arrived"
        and a.status != "arrived"
        and a.arrived_at is None
    ):
        a.arrived_at = utcnow()
        arrival_change = {"old": None, "new": _iso(a.arrived_at), "source": "manual"}

    # The edit form resends start_at/end_at on every save, so "named in the
    # request" is not "changed": Undo arrival's edited-since test reads this.
    from gdx_dispatch.services.visit_sync import to_minute  # noqa: PLC0415

    times_changed = [
        f for f in ("start_at", "end_at")
        if f in data and to_minute(_aware(data[f])) != to_minute(_aware(getattr(a, f)))
    ]
    for field in (
        "tech_id",
        "title",
        "description",
        "address",
        "start_at",
        "end_at",
        "notes",
        "status",
    ):
        if field in data:
            setattr(a, field, data[field])
    # Relinks: empty string / null clears; malformed uuid refuses rather than
    # silently unlinking.
    for field in ("job_id", "customer_id"):
        if field in data:
            if data[field]:
                try:
                    setattr(a, field, UUID(str(data[field])))
                except ValueError:
                    raise HTTPException(status_code=422, detail=f"{field} must be a UUID") from None
            else:
                setattr(a, field, None)
    for field in ("lat", "lng"):
        if field in data:
            val = data[field]
            setattr(a, field, Decimal(str(val)) if val is not None else None)

    details: dict[str, Any] = {"fields": list(data.keys()), "times_changed": times_changed}
    if arrival_change is not None:
        details["arrived_at"] = arrival_change
    _record(
        db, a, tenant_id=tenant_id, user=user, action="appointment_updated",
        details=details, request=request, job_ids=(old_job_id, a.job_id),
    )
    return _serialize(a)


@router.delete("/api/appointments/{appt_id}", response_model=None, status_code=204)
def delete_appointment(
    appt_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    a = _get_scoped(db, appt_id, tenant_id)
    if a.job_id is not None and a.arrived_at is not None:
        return _conflict(
            "This visit has an arrival recorded, so deleting it would drop a worked "
            "day. Use Undo arrival for a mis-tap, or Cancel for a duplicate the crew worked.",
            "visit_arrived",
            visit_id=str(a.id),
        )
    a.deleted_at = utcnow()
    _record(
        db, a, tenant_id=tenant_id, user=user, action="appointment_deleted",
        request=request, job_ids=(a.job_id,),
    )
    return None


@router.post("/api/appointments/{appt_id}/confirm", response_model=None)
def confirm_appointment(
    appt_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    a = _get_scoped(db, appt_id, tenant_id)
    if a.status in ("completed", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot confirm appointment in status '{a.status}'",
        )
    a.status = "confirmed"
    a.confirmed_at = utcnow()
    _record(
        db, a, tenant_id=tenant_id, user=user, action="appointment_confirmed",
        request=request, job_ids=(a.job_id,),
    )
    return _serialize(a)


@router.post("/api/appointments/{appt_id}/on-my-way", response_model=None)
def on_my_way(
    appt_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    a = _get_scoped(db, appt_id, tenant_id)
    if a.status in ("completed", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot mark en_route in status '{a.status}'",
        )
    a.status = "en_route"
    a.en_route_at = utcnow()
    _record(
        db, a, tenant_id=tenant_id, user=user, action="appointment_en_route",
        request=request, job_ids=(a.job_id,),
    )
    return _serialize(a)


@router.post("/api/appointments/{appt_id}/arrived", response_model=None)
def arrived(
    appt_id: UUID,
    request: Request,
    payload: ArrivedIn | None = None,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Office Arrived. Records a time (supplied, else now) and keeps one the
    tech already stamped; a time for a visit that has one is a correction,
    which goes through the PATCH (multi-day jobs plan §5.2a)."""
    tenant_id = _tenant_id(request)
    a = _get_scoped(db, appt_id, tenant_id)
    if a.status in ("completed", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot mark arrived in status '{a.status}'",
        )
    supplied = payload.arrived_at if payload else None
    if supplied is not None and supplied.tzinfo is None:
        supplied = supplied.replace(tzinfo=timezone.utc)
    if supplied is not None and supplied > utcnow() + timedelta(minutes=5):
        raise HTTPException(status_code=422, detail="arrived_at is in the future")
    if supplied is not None and a.arrived_at is not None:
        return _conflict(
            f"An arrival is already recorded at {a.arrived_at.isoformat()}. "
            "Correct it by editing the visit.",
            "arrival_recorded",
            visit_id=str(a.id),
            arrived_at=a.arrived_at.isoformat(),
        )
    details: dict[str, Any] = {"source": "manual"}
    if a.arrived_at is None:
        a.arrived_at = supplied or utcnow()
        details["arrived_at"] = _iso(a.arrived_at)
    else:
        details["kept_arrived_at"] = _iso(a.arrived_at)
    a.status = "arrived"
    _record(
        db, a, tenant_id=tenant_id, user=user, action="appointment_arrived",
        details=details, request=request, job_ids=(a.job_id,),
    )
    return _serialize(a)


@router.post("/api/appointments/{appt_id}/complete", response_model=None)
def complete_appointment(
    appt_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    a = _get_scoped(db, appt_id, tenant_id)
    if a.status == "cancelled":
        raise HTTPException(
            status_code=400, detail="Cannot complete a cancelled appointment"
        )
    a.status = "completed"
    a.completed_at = utcnow()
    _record(
        db, a, tenant_id=tenant_id, user=user, action="appointment_completed",
        request=request, job_ids=(a.job_id,),
    )
    return _serialize(a)


@router.post("/api/appointments/{appt_id}/cancel", response_model=None)
def cancel_appointment(
    appt_id: UUID,
    request: Request,
    payload: CancelIn | None = None,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    a = _get_scoped(db, appt_id, tenant_id)
    if a.status == "completed":
        raise HTTPException(
            status_code=400, detail="Cannot cancel a completed appointment"
        )
    if _status_only_arrival(a):
        return _ask_about_status_only(a)
    reason = (payload.reason if payload else None) or None
    a.status = "cancelled"
    if reason:
        existing = (a.notes or "").strip()
        suffix = f"cancel reason: {reason}"
        a.notes = f"{existing}\n{suffix}".strip() if existing else suffix
    _record(
        db, a, tenant_id=tenant_id, user=user, action="appointment_cancelled",
        details={"reason": reason} if reason else {},
        request=request, job_ids=(a.job_id,),
    )
    return _serialize(a)


# ---------------------------------------------------------------- undo arrival
# Multi-day jobs plan §5.2a, *Undo arrival*: asked first, reason required;
# the logic lives in services/arrival_undo.py.


class UndoArrivalIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    tap_record_id: str | None = Field(default=None, max_length=64)
    also_undo: list[str] = Field(default_factory=list, max_length=50)


class UndoJobTapsIn(BaseModel):
    tech_id: str = Field(min_length=1, max_length=64)
    tap_record_ids: list[str] = Field(default_factory=list, max_length=50)
    reason: str = Field(min_length=1, max_length=1000)


# Undo arrival is a dispatch-manager action (owner, admin, dispatcher, manager).
_office = require_role(*sorted(DISPATCH_MANAGER_ROLES))


def _job_of(db: Session, job_id: Any) -> Any:
    from gdx_dispatch.models.tenant_models import Job  # noqa: PLC0415

    if job_id is None:
        return None
    job = db.get(Job, job_id)
    return job if job is not None and job.deleted_at is None else None


def _commit_or_refuse(db: Session, run: Any) -> Any:
    """Run one undo; commit it, or roll it back and answer its refusal."""
    from gdx_dispatch.services.arrival_undo import UndoRefused  # noqa: PLC0415

    try:
        result = run()
    except UndoRefused as refused:
        db.rollback()
        return JSONResponse(refused.body(), status_code=refused.status)
    db.commit()
    return result


@router.get("/api/appointments/{appt_id}/undo-arrival", response_model=None, dependencies=[Depends(_office)])
def preview_undo_visit_arrival(
    appt_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """What the Undo arrival dialog asks about for this visit."""
    from gdx_dispatch.services.arrival_undo import preview_visit  # noqa: PLC0415

    a = _get_scoped(db, appt_id, _tenant_id(request))
    return preview_visit(db, _job_of(db, a.job_id), a)


@router.post("/api/appointments/{appt_id}/undo-arrival", response_model=None, dependencies=[Depends(_office)])
def undo_visit_arrival(
    appt_id: UUID,
    payload: UndoArrivalIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    from gdx_dispatch.services.arrival_undo import undo_visit_arrival as _undo  # noqa: PLC0415

    if not payload.reason.strip():
        raise HTTPException(status_code=422, detail="A reason is required")
    a = _get_scoped(db, appt_id, _tenant_id(request))
    result = _commit_or_refuse(db, lambda: _undo(
        db, _job_of(db, a.job_id), a, user_id=_user_id(user), reason=payload.reason.strip(),
        tap_record_id=payload.tap_record_id, also_undo=payload.also_undo,
    ))
    if isinstance(result, JSONResponse):
        return result
    db.refresh(a)
    return {"appointment": _serialize(a), **result}


@router.get("/api/jobs/{job_id}/undo-arrival", response_model=None, dependencies=[Depends(_office)])
def preview_undo_job_taps(
    job_id: UUID,
    tech_id: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The crew-row dialog: this tech's taps on the job that no visit holds."""
    from gdx_dispatch.services.arrival_undo import preview_job  # noqa: PLC0415

    job = _job_of(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return preview_job(db, job, tech_id)


@router.post("/api/jobs/{job_id}/undo-arrival", response_model=None, dependencies=[Depends(_office)])
def undo_job_taps(
    job_id: UUID,
    payload: UndoJobTapsIn,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    from gdx_dispatch.services.arrival_undo import undo_job_taps as _undo  # noqa: PLC0415

    if not payload.reason.strip():
        raise HTTPException(status_code=422, detail="A reason is required")
    job = _job_of(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    result = _commit_or_refuse(db, lambda: _undo(
        db, job, tech_id=payload.tech_id, tap_record_ids=payload.tap_record_ids,
        user_id=_user_id(user), reason=payload.reason.strip(),
    ))
    if isinstance(result, JSONResponse):
        return result
    return {"job_id": str(job.id), **result}
