"""
Scheduling router — calendar views, availability slots, conflicts, and
recurring-schedule expansion. This is a read-heavy view layer built on top
of the existing Appointment model (gdx_dispatch/routers/appointments.py) and the
recurring schedules table owned by gdx_dispatch/routers/recurring_jobs.py.

Owns exactly ONE new entity: TechUnavailability (vacation/sick/other).

Gated behind the "jobs" module. Every query is tenant-scoped via
request.state.tenant["id"]. Mutations write audit events.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.appointments import Appointment
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["scheduling"],
    dependencies=[Depends(require_module("jobs"))],
)


# ---------------------------------------------------------------------------
# Model (one new table)
# ---------------------------------------------------------------------------


from gdx_dispatch.models.tenant_models import TechUnavailability  # noqa: E402

# ---------------------------------------------------------------------------
# Pydantic
# ---------------------------------------------------------------------------


class TechUnavailabilityIn(BaseModel):
    tech_id: str = Field(min_length=1, max_length=64)
    start_at: datetime
    end_at: datetime
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _validate_range(self) -> TechUnavailabilityIn:
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


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


def _parse_date(raw: str | None, field: str) -> date:
    if not raw:
        raise HTTPException(status_code=422, detail=f"Missing {field}")
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail=f"Invalid {field} (YYYY-MM-DD)") from None


def _parse_iso_datetime(raw: str | None, field: str) -> datetime:
    if not raw:
        raise HTTPException(status_code=422, detail=f"Missing {field}")
    try:
        if "T" in raw or " " in raw:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail=f"Invalid {field} (ISO format)") from None


def _day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def _serialize_appt(a: Appointment) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "job_id": str(a.job_id) if a.job_id else None,
        "customer_id": str(a.customer_id) if a.customer_id else None,
        "tech_id": a.tech_id,
        "title": a.title,
        "description": a.description,
        "address": a.address,
        "start_at": a.start_at.isoformat() if a.start_at else None,
        "end_at": a.end_at.isoformat() if a.end_at else None,
        "status": a.status,
    }


def _serialize_unavail(u: TechUnavailability) -> dict[str, Any]:
    return {
        "id": str(u.id),
        "company_id": u.company_id,
        "tech_id": u.tech_id,
        "start_at": u.start_at.isoformat() if u.start_at else None,
        "end_at": u.end_at.isoformat() if u.end_at else None,
        "reason": u.reason,
        "created_by": u.created_by,
        "created_at": u.created_at.isoformat() if u.created_at else None,
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
            "scheduling_audit_failed action=%s entity_id=%s", action, entity_id
        )
        db.rollback()


# ---------------------------------------------------------------------------
# Calendar views (read-only)
# ---------------------------------------------------------------------------


@router.get("/api/calendar/month", response_model=None)
def calendar_month(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    year: int = 2026,
    month: int = 1,
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    if not (1 <= int(month) <= 12):
        raise HTTPException(status_code=422, detail="Invalid month")
    if not (1970 <= int(year) <= 2999):
        raise HTTPException(status_code=422, detail="Invalid year")

    first = date(int(year), int(month), 1)
    next_first = date(int(year) + 1, 1, 1) if month == 12 else date(int(year), int(month) + 1, 1)

    start_dt = datetime.combine(first, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(next_first, time.min, tzinfo=timezone.utc)

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    rows = db.execute(
        select(Appointment).where(
            Appointment.deleted_at.is_(None),
            Appointment.start_at >= start_dt,
            Appointment.start_at < end_dt,
        )
    ).scalars().all()

    buckets: dict[str, dict[str, Any]] = {}
    d = first
    while d < next_first:
        buckets[d.isoformat()] = {
            "date": d.isoformat(),
            "appointment_count": 0,
            "statuses": {},
        }
        d += timedelta(days=1)

    for a in rows:
        key = a.start_at.date().isoformat()
        bucket = buckets.get(key)
        if bucket is None:
            continue
        bucket["appointment_count"] += 1
        bucket["statuses"][a.status] = bucket["statuses"].get(a.status, 0) + 1

    return [buckets[k] for k in sorted(buckets.keys())]


@router.get("/api/calendar/week", response_model=None)
def calendar_week(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    date: str | None = None,
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    ref = _parse_date(date, "date")
    # Monday of the week containing `ref`
    monday = ref - timedelta(days=ref.weekday())
    sunday_plus_one = monday + timedelta(days=7)

    start_dt = datetime.combine(monday, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(sunday_plus_one, time.min, tzinfo=timezone.utc)

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    rows = db.execute(
        select(Appointment).where(
            Appointment.deleted_at.is_(None),
            Appointment.start_at >= start_dt,
            Appointment.start_at < end_dt,
        )
    ).scalars().all()

    buckets: dict[str, dict[str, Any]] = {}
    for i in range(7):
        d = monday + timedelta(days=i)
        buckets[d.isoformat()] = {
            "date": d.isoformat(),
            "appointment_count": 0,
            "statuses": {},
        }

    for a in rows:
        key = a.start_at.date().isoformat()
        bucket = buckets.get(key)
        if bucket is None:
            continue
        bucket["appointment_count"] += 1
        bucket["statuses"][a.status] = bucket["statuses"].get(a.status, 0) + 1

    return [buckets[k] for k in sorted(buckets.keys())]


@router.get("/api/calendar/events", response_model=None)
def calendar_events(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    start: str | None = None,
    end: str | None = None,
    tech_id: str | None = None,
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    start_dt = _parse_iso_datetime(start, "start")
    end_dt = _parse_iso_datetime(end, "end")
    if end_dt <= start_dt:
        raise HTTPException(status_code=422, detail="end must be after start")

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = select(Appointment).where(
        Appointment.deleted_at.is_(None),
        Appointment.start_at >= start_dt,
        Appointment.start_at < end_dt,
    )
    if tech_id:
        stmt = stmt.where(Appointment.tech_id == tech_id)
    stmt = stmt.order_by(Appointment.start_at.asc())
    rows = db.execute(stmt).scalars().all()
    return [_serialize_appt(r) for r in rows]


@router.get("/api/calendar/technician/{tech_id}", response_model=None)
def calendar_technician(
    tech_id: str,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    start_dt = _parse_iso_datetime(start, "start")
    end_dt = _parse_iso_datetime(end, "end")
    if end_dt <= start_dt:
        raise HTTPException(status_code=422, detail="end must be after start")
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    rows = db.execute(
        select(Appointment)
        .where(
            Appointment.deleted_at.is_(None),
            Appointment.tech_id == tech_id,
            Appointment.start_at >= start_dt,
            Appointment.start_at < end_dt,
        )
        .order_by(Appointment.start_at.asc())
    ).scalars().all()
    return [_serialize_appt(r) for r in rows]


# ---------------------------------------------------------------------------
# Availability / conflicts
# ---------------------------------------------------------------------------


@router.get("/api/appointments/available-slots", response_model=None)
def available_slots(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    tech_id: str | None = None,
    date: str | None = None,
    duration_minutes: int = 60,
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    if not tech_id:
        raise HTTPException(status_code=422, detail="tech_id required")
    d = _parse_date(date, "date")
    duration = max(15, min(int(duration_minutes or 60), 480))

    day_start, day_end = _day_bounds(d)
    # 09:00 - 18:00 UTC
    work_start = day_start.replace(hour=9)
    work_end = day_start.replace(hour=18)

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    appts = db.execute(
        select(Appointment).where(
            Appointment.deleted_at.is_(None),
            Appointment.tech_id == tech_id,
            Appointment.start_at < day_end,
            Appointment.end_at > day_start,
        )
    ).scalars().all()

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    unavail = db.execute(
        select(TechUnavailability).where(
            TechUnavailability.deleted_at.is_(None),
            TechUnavailability.tech_id == tech_id,
            TechUnavailability.start_at < day_end,
            TechUnavailability.end_at > day_start,
        )
    ).scalars().all()

    # SQLite strips tzinfo on round-trip. Normalize all rows to UTC-aware so
    # comparisons with the UTC-aware slot cursor don't blow up in tests.
    def _aware(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    slots: list[dict[str, Any]] = []
    cursor = work_start
    delta = timedelta(minutes=duration)
    while cursor + delta <= work_end:
        slot_end = cursor + delta
        blocked = False
        for a in appts:
            a_start, a_end = _aware(a.start_at), _aware(a.end_at)
            if a_start and a_end and a_start < slot_end and a_end > cursor:
                blocked = True
                break
        if not blocked:
            for u in unavail:
                u_start, u_end = _aware(u.start_at), _aware(u.end_at)
                if u_start and u_end and u_start < slot_end and u_end > cursor:
                    blocked = True
                    break
        if not blocked:
            slots.append(
                {
                    "start_at": cursor.isoformat(),
                    "end_at": slot_end.isoformat(),
                }
            )
        cursor = slot_end
    return slots


@router.get("/api/schedule/conflicts", response_model=None)
def schedule_conflicts(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    date: str | None = None,
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    d = _parse_date(date, "date")
    day_start, day_end = _day_bounds(d)

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    rows = db.execute(
        select(Appointment)
        .where(
            Appointment.deleted_at.is_(None),
            Appointment.tech_id.is_not(None),
            Appointment.start_at < day_end,
            Appointment.end_at > day_start,
        )
        .order_by(Appointment.tech_id.asc(), Appointment.start_at.asc())
    ).scalars().all()

    by_tech: dict[str, list[Appointment]] = {}
    for a in rows:
        by_tech.setdefault(a.tech_id, []).append(a)

    conflicts: list[dict[str, Any]] = []
    for tid, appts in by_tech.items():
        for i in range(len(appts)):
            for j in range(i + 1, len(appts)):
                a, b = appts[i], appts[j]
                if a.start_at < b.end_at and a.end_at > b.start_at:
                    conflicts.append(
                        {
                            "tech_id": tid,
                            "appointment_a_id": str(a.id),
                            "appointment_b_id": str(b.id),
                        }
                    )
    return conflicts


# ---------------------------------------------------------------------------
# Tech unavailability CRUD
# ---------------------------------------------------------------------------


@router.get("/api/tech-unavailability", response_model=None)
def list_tech_unavailability(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    tech_id: str | None = None,
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = select(TechUnavailability).where(
        TechUnavailability.deleted_at.is_(None),
    )
    if tech_id:
        stmt = stmt.where(TechUnavailability.tech_id == tech_id)
    stmt = stmt.order_by(TechUnavailability.start_at.desc())
    rows = db.execute(stmt).scalars().all()
    return [_serialize_unavail(r) for r in rows]


@router.post("/api/tech-unavailability", response_model=None, status_code=201)
def create_tech_unavailability(
    payload: TechUnavailabilityIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    row = TechUnavailability(
        company_id=tenant_id,
        tech_id=payload.tech_id,
        start_at=payload.start_at,
        end_at=payload.end_at,
        reason=payload.reason,
        created_by=_user_id(user),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="tech_unavailability_created",
        entity_type="tech_unavailability",
        entity_id=str(row.id),
        details={"tech_id": row.tech_id, "reason": row.reason},
        request=request,
    )
    return _serialize_unavail(row)


@router.delete("/api/tech-unavailability/{unavail_id}", response_model=None, status_code=204)
def delete_tech_unavailability(
    unavail_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    row = db.execute(
        select(TechUnavailability).where(
            TechUnavailability.id == unavail_id,
            TechUnavailability.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Unavailability not found")
    row.deleted_at = utcnow()
    db.commit()
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="tech_unavailability_deleted",
        entity_type="tech_unavailability",
        entity_id=str(unavail_id),
        request=request,
    )
    return None


# ---------------------------------------------------------------------------
# Recurring schedule expansion
# ---------------------------------------------------------------------------


@router.post("/api/recurring-schedules/{schedule_id}/generate", response_model=None)
def generate_recurring_schedule(
    schedule_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    horizon_days: int = 30,
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    horizon_days = max(1, min(int(horizon_days or 30), 365))

    # The recurring_jobs router owns the recurring_job_schedules table. We
    # read-only-query it here; if it or the row doesn't exist, return
    # {expanded: 0} rather than blowing up.
    schedule: dict[str, Any] | None = None
    try:
        from sqlalchemy import select

        from gdx_dispatch.models.tenant_models import RecurringJobSchedule
        sched = db.execute(
            select(RecurringJobSchedule).where(
                RecurringJobSchedule.id == str(schedule_id),
                RecurringJobSchedule.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if sched:
            schedule = {
                "id": sched.id, "job_template_id": sched.job_template_id,
                "frequency": sched.frequency, "customer_id": sched.customer_id,
                "next_run": sched.next_run, "status": sched.status,
            }
    except Exception:
        log.exception(
            "scheduling_recurring_lookup_failed schedule_id=%s", schedule_id
        )
        return {"expanded": 0, "appointment_ids": []}

    if not schedule or str(schedule.get("status") or "").lower() != "active":
        return {"expanded": 0, "appointment_ids": []}

    try:
        next_run_raw = schedule.get("next_run")
        if isinstance(next_run_raw, datetime):
            next_run = next_run_raw
        else:
            next_run = datetime.fromisoformat(str(next_run_raw).replace("Z", "+00:00"))
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        log.exception(
            "scheduling_recurring_bad_next_run schedule_id=%s", schedule_id
        )
        return {"expanded": 0, "appointment_ids": []}

    frequency = str(schedule.get("frequency") or "").lower()
    step_days = {
        "weekly": 7,
        "biweekly": 14,
        "monthly": 30,
        "quarterly": 90,
    }.get(frequency)
    if not step_days:
        return {"expanded": 0, "appointment_ids": []}

    horizon_end = datetime.now(timezone.utc) + timedelta(days=horizon_days)
    created_ids: list[str] = []
    occurrence = next_run
    # Cap iterations to avoid runaway if next_run is far in the past.
    max_iters = horizon_days + 10
    iters = 0
    while occurrence <= horizon_end and iters < max_iters:
        iters += 1
        appt = Appointment(
            company_id=tenant_id,
            job_id=None,
            customer_id=None,
            tech_id=None,
            title=f"Recurring: {schedule.get('job_template_id') or schedule_id}",
            description=None,
            address=None,
            lat=None,
            lng=None,
            start_at=occurrence,
            end_at=occurrence + timedelta(hours=1),
            status="scheduled",
            notes=None,
            created_by=_user_id(user),
        )
        db.add(appt)
        db.flush()
        created_ids.append(str(appt.id))
        occurrence = occurrence + timedelta(days=step_days)

    db.commit()
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="recurring_schedule_expanded",
        entity_type="recurring_schedule",
        entity_id=str(schedule_id),
        details={"expanded": len(created_ids), "horizon_days": horizon_days},
        request=request,
    )
    return {"expanded": len(created_ids), "appointment_ids": created_ids}
