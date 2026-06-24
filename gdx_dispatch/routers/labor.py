from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.permissions import is_dispatch_manager
from gdx_dispatch.models.tenant_models import Job, Technician, TimeEntry
from gdx_dispatch.modules.inventory.models import JobPart

try:
    from gdx_dispatch.routers.auth import get_current_user
except ImportError:
    logging.getLogger(__name__).exception("labor_auth_import_failed_using_fallback")

    async def get_current_user(token: str) -> dict[str, Any]:
        _ = token
        return {}

router = APIRouter(prefix="/api", tags=["labor"], dependencies=[Depends(require_module("timeclock"))])
log = logging.getLogger(__name__)

DEFAULT_HOURLY_RATE = 50.0
OVERHEAD_RATE = 0.08


class TimeEntryCreate(BaseModel):
    tech_id: str = Field(min_length=1)
    clock_in: datetime
    clock_out: datetime
    entry_type: str = Field(default="manual", min_length=1)


class TimeEntryPatch(BaseModel):
    tech_id: str | None = None
    clock_in: datetime | None = None
    clock_out: datetime | None = None
    entry_type: str | None = None


async def _current_user_dependency(request: Request) -> dict[str, Any]:
    # OAuth2PasswordBearer can deadlock in some test runtimes; parse bearer token directly.
    auth_header = request.headers.get("authorization", "")
    token = ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await get_current_user(request, token)


def _require_dispatch(user: dict[str, Any] = Depends(_current_user_dependency)) -> dict[str, Any]:
    """Back-office labor management is dispatch/admin-only. Technicians record
    their own time via the self-scoped /api/timeclock endpoints, not here —
    these endpoints take an arbitrary tech_id and expose tenant-wide cost data."""
    if not is_dispatch_manager(user):
        raise HTTPException(status_code=403, detail="dispatcher or admin role required")
    return user


def _to_float(value: Decimal | float | int | None) -> float:
    if value is None:
        return 0.0
    return float(value)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _duration_minutes(clock_in: datetime | None, clock_out: datetime | None) -> int | None:
    start = _as_utc(clock_in)
    end = _as_utc(clock_out)
    if not start or not end:
        return None
    return int((end - start).total_seconds() / 60)


def _resolve_hourly_rate(db: Session, tech_id: str) -> float:
    try:
        tech = db.execute(
            select(Technician).where(Technician.id == tech_id).limit(1)
        ).scalar_one_or_none()
    except SQLAlchemyError:
        log.exception("resolve_hourly_rate_failed", extra={"tech_id": tech_id})
        tech = None
    if not tech:
        return DEFAULT_HOURLY_RATE
    return DEFAULT_HOURLY_RATE if tech.hourly_rate is None else float(tech.hourly_rate)


def _entry_cost(entry: TimeEntry) -> float:
    minutes = entry.duration_minutes or 0
    rate = _to_float(entry.hourly_rate) or DEFAULT_HOURLY_RATE
    return round((minutes / 60.0) * rate, 2)


def _entry_to_dict(entry: TimeEntry) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "job_id": str(entry.job_id),
        "tech_id": entry.tech_id,
        "clock_in": entry.clock_in.isoformat() if entry.clock_in else None,
        "clock_out": entry.clock_out.isoformat() if entry.clock_out else None,
        "duration_minutes": entry.duration_minutes,
        "entry_type": entry.entry_type,
        "hourly_rate": _to_float(entry.hourly_rate),
        "labor_cost": _entry_cost(entry),
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        "deleted_at": entry.deleted_at.isoformat() if entry.deleted_at else None,
    }


def _get_job_or_404(db: Session, job_id: UUID) -> Job:
    row = db.get(Job, job_id)
    if not row or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


@router.get("/jobs/{job_id}/time-entries", response_model=None)
def list_job_time_entries(
    job_id: UUID,
    _: dict[str, Any] = Depends(_require_dispatch),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    _get_job_or_404(db, job_id)
    rows = (
        db.query(TimeEntry)
        .filter(TimeEntry.job_id == job_id, TimeEntry.deleted_at.is_(None))
        .order_by(TimeEntry.clock_in.desc())
        .all()
    )
    return [_entry_to_dict(row) for row in rows]


@router.post("/jobs/{job_id}/time-entries", response_model=None, status_code=201)
def create_job_time_entry(
    request: Request,
    job_id: UUID,
    payload: TimeEntryCreate,
    _: dict[str, Any] = Depends(_require_dispatch),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _get_job_or_404(db, job_id)
    payload_clock_in = _as_utc(payload.clock_in)
    payload_clock_out = _as_utc(payload.clock_out)
    if payload_clock_out and payload_clock_in and payload_clock_out <= payload_clock_in:
        raise HTTPException(status_code=422, detail="clock_out must be after clock_in")

    _tid = str((getattr(request.state, "tenant", {}) or {}).get("id") or "tenant-test")
    row = TimeEntry(
        company_id=_tid,
        job_id=job_id,
        tech_id=payload.tech_id.strip(),
        clock_in=payload_clock_in,
        clock_out=payload_clock_out,
        duration_minutes=_duration_minutes(payload_clock_in, payload_clock_out),
        entry_type=payload.entry_type.strip(),
        hourly_rate=_resolve_hourly_rate(db, payload.tech_id.strip()),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    log.info("time_entry_created", extra={"job_id": str(job_id), "tech_id": payload.tech_id.strip()})
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="create_job_time_entry",
                entity_type="job_time_entry",
                entity_id=str(job_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('create_job_time_entry_audit_failed')
    return _entry_to_dict(row)


@router.patch("/time-entries/{entry_id}", response_model=None)
def update_time_entry(
    entry_id: UUID,
    payload: TimeEntryPatch,
    _: dict[str, Any] = Depends(_require_dispatch),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.get(TimeEntry, entry_id)
    if not row or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Time entry not found")

    updates = payload.model_dump(exclude_unset=True)
    clock_in = _as_utc(updates.get("clock_in", row.clock_in))
    clock_out = _as_utc(updates.get("clock_out", row.clock_out))
    if clock_in and clock_out and clock_out <= clock_in:
        raise HTTPException(status_code=422, detail="clock_out must be after clock_in")

    if "tech_id" in updates:
        tech_id = (updates["tech_id"] or "").strip()
        if not tech_id:
            raise HTTPException(status_code=422, detail="tech_id cannot be empty")
        row.tech_id = tech_id
        row.hourly_rate = _resolve_hourly_rate(db, tech_id)
    if "clock_in" in updates:
        row.clock_in = _as_utc(updates["clock_in"])
    if "clock_out" in updates:
        row.clock_out = _as_utc(updates["clock_out"])
    if "entry_type" in updates:
        entry_type = (updates["entry_type"] or "").strip()
        if not entry_type:
            raise HTTPException(status_code=422, detail="entry_type cannot be empty")
        row.entry_type = entry_type

    row.duration_minutes = _duration_minutes(row.clock_in, row.clock_out)
    row.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(row)
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="update_time_entry",
                entity_type="time_entry",
                entity_id=str(entry_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('update_time_entry_audit_failed')
    return _entry_to_dict(row)


@router.delete("/time-entries/{entry_id}", response_model=None)
def delete_time_entry(
    entry_id: UUID,
    _: dict[str, Any] = Depends(_require_dispatch),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    row = db.get(TimeEntry, entry_id)
    if not row or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Time entry not found")
    now = datetime.now(UTC)
    row.deleted_at = now
    row.updated_at = now
    db.commit()
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="delete_time_entry",
                entity_type="time_entry",
                entity_id=str(entry_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('delete_time_entry_audit_failed')
    return {"deleted": True}


@router.get("/jobs/{job_id}/costing", response_model=None, operation_id="get_job_labor_costing")
def get_job_labor_costing(
    job_id: UUID,
    _: dict[str, Any] = Depends(_require_dispatch),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _get_job_or_404(db, job_id)
    entries = (
        db.query(TimeEntry)
        .filter(TimeEntry.job_id == job_id, TimeEntry.deleted_at.is_(None))
        .all()
    )
    labor_cost = round(sum(_entry_cost(entry) for entry in entries), 2)
    try:
        from sqlalchemy import func as _func
        materials_row = db.execute(
            select(
                _func.coalesce(
                    _func.sum(
                        _func.coalesce(JobPart.qty_used, 0) * _func.coalesce(JobPart.unit_cost_at_time, 0)
                    ), 0
                ).label("materials_cost")
            ).where(JobPart.job_id == job_id)
        ).mappings().first()
    except SQLAlchemyError:
        log.exception("materials_cost_query_failed", extra={"job_id": str(job_id)})
        materials_row = None
    materials_cost = round(float((materials_row or {}).get("materials_cost", 0) or 0), 2)
    overhead_cost = round((labor_cost + materials_cost) * OVERHEAD_RATE, 2)
    total_cost = round(labor_cost + materials_cost + overhead_cost, 2)

    return {
        "job_id": str(job_id),
        "labor_cost": labor_cost,
        "materials_cost": materials_cost,
        "overhead_cost": overhead_cost,
        "total_cost": total_cost,
        "labor_minutes": int(sum(entry.duration_minutes or 0 for entry in entries)),
    }


@router.get("/reports/labor-summary", response_model=None)
def labor_summary(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    _: dict[str, Any] = Depends(_require_dispatch),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    now = datetime.now(UTC).date()
    resolved_end = end_date or now
    resolved_start = start_date or (resolved_end - timedelta(days=29))
    if resolved_start > resolved_end:
        raise HTTPException(status_code=422, detail="start_date must be on or before end_date")

    start_dt = datetime.combine(resolved_start, time.min, tzinfo=UTC)
    end_dt_exclusive = datetime.combine(resolved_end + timedelta(days=1), time.min, tzinfo=UTC)

    rows = (
        db.query(TimeEntry)
        .filter(
            TimeEntry.deleted_at.is_(None),
            TimeEntry.clock_in >= start_dt,
            TimeEntry.clock_in < end_dt_exclusive,
        )
        .all()
    )

    totals: dict[str, dict[str, float]] = defaultdict(lambda: {"minutes": 0.0, "cost": 0.0})
    for row in rows:
        tech_id = row.tech_id
        minutes = float(row.duration_minutes or 0)
        cost = _entry_cost(row)
        totals[tech_id]["minutes"] += minutes
        totals[tech_id]["cost"] += cost

    items = [
        {
            "tech_id": tech_id,
            "hours": round(values["minutes"] / 60.0, 2),
            "cost": round(values["cost"], 2),
        }
        for tech_id, values in sorted(totals.items(), key=lambda item: item[0])
    ]
    total_hours = round(sum(item["hours"] for item in items), 2)
    total_cost = round(sum(item["cost"] for item in items), 2)

    return {
        "start_date": resolved_start.isoformat(),
        "end_date": resolved_end.isoformat(),
        "total_hours": total_hours,
        "total_cost": total_cost,
        "items": items,
    }
