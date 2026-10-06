"""A job's visits, as the office books them (multi-day jobs plan §5.3a).

A visit is an ``appointments`` row with ``job_id`` set. The board and the job
form book visits as a side effect of a date or crew edit (``visit_sync``);
these routes book them directly: "Add day(s)" on the job page, and Move and
Remove on its Visits card.

  GET    /api/jobs/{job_id}/visits                   every visit, oldest first
  POST   /api/jobs/{job_id}/visits                   book days
  PATCH  /api/jobs/{job_id}/visits/{visit_id}        move an OPEN visit (day, time, length, tech)
  DELETE /api/jobs/{job_id}/visits/{visit_id}        retire an OPEN visit

Every write is planned by a pure function in ``services/visit_sync.py`` —
a refusal answers 409 and writes nothing — then applied with an audit row
per visit, and ends with ``recompute_job_schedule`` (invariant I).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.audit import ensure_audit_table
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.core.pay_periods import shop_today
from gdx_dispatch.core.permissions import is_dispatch_manager
from gdx_dispatch.models.tenant_models import Job, Technician
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.services import visit_sync as vs

router = APIRouter(
    prefix="/api",
    tags=["job-visits"],
    dependencies=[Depends(require_module("jobs"))],
)


def _uid(user: dict) -> str | None:
    return str(user.get("sub") or user.get("user_id") or "") or None


def _require_dispatch_role(user: dict) -> None:
    # The same gate as a crew change (job_assignments.py): booking a day is
    # the dispatcher's call, not a technician's.
    if not is_dispatch_manager(user):
        raise HTTPException(status_code=403, detail="booking visits requires dispatcher, admin, or owner role")


def _load_job(db: Session, job_id: str) -> Job:
    try:
        job = db.get(Job, UUID(str(job_id)))
    except ValueError:
        job = None
    if job is None or job.deleted_at is not None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def _writable_job(db: Session, user: dict, job_id: str) -> Job:
    _require_dispatch_role(user)
    ensure_audit_table(db)  # before staging: its first run on an engine commits
    return _load_job(db, job_id)


def _parse_visit_id(visit_id: str) -> UUID:
    try:
        return UUID(str(visit_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="visit not found") from None


def _check_techs(db: Session, tech_ids: list) -> None:
    wanted = {str(t) for t in tech_ids if t is not None}
    if not wanted:
        return
    found = {str(t) for t in db.execute(select(Technician.id).where(Technician.id.in_(wanted))).scalars().all()}
    missing = sorted(wanted - found)
    if missing:
        raise HTTPException(status_code=422, detail=f"unknown technician: {', '.join(missing)}")


def _refused(refusal: vs.Refusal) -> JSONResponse:
    status = 404 if refusal.code == "visit_not_found" else 409
    return JSONResponse({"detail": refusal.message, "code": refusal.code, **refusal.detail}, status_code=status)


def _iso(value: datetime | None) -> str | None:
    return vs._as_utc(value).isoformat() if value is not None else None


def _visit_list(db: Session, job: Job) -> dict[str, Any]:
    tz_name = vs.shop_tz(db)
    rows = vs.visit_rows(db, job.id)
    tech_ids = {r.tech_id for r in rows if r.tech_id}
    names = {
        str(t.id): t.name for t in db.execute(
            select(Technician).where(Technician.id.in_(tech_ids))
        ).scalars().all()
    } if tech_ids else {}
    work_days = sorted({vs.shop_day(r.start_at, tz_name) for r in rows if vs.is_live(r)})
    index = {d: i + 1 for i, d in enumerate(work_days)}
    items = []
    for r in rows:
        day = vs.shop_day(r.start_at, tz_name)
        items.append({
            "id": str(r.id),
            "tech_id": r.tech_id,
            "tech_name": names.get(str(r.tech_id)) if r.tech_id else None,
            "start_at": _iso(r.start_at),
            "end_at": _iso(r.end_at),
            "status": r.status,
            "arrived_at": _iso(r.arrived_at),
            "completed_at": _iso(r.completed_at),
            "state": vs.visit_state(r),
            "day": day.isoformat() if day else None,
            # A cancelled visit is listed but is not a day of work.
            "day_index": index.get(day) if vs.is_live(r) else None,
        })
    return {"items": items, "day_count": len(work_days), "timezone": tz_name}


def _apply(db: Session, job: Job, plan: vs.VisitPlan, user: dict, reason: str) -> dict[str, Any]:
    uid = _uid(user)
    vs.apply_visit_plan(db, job, plan, uid)
    vs.recompute_job_schedule(db, job, uid, reason)
    db.commit()
    return _visit_list(db, job)


def _values(db: Session, job: Job) -> list[vs.VisitRow]:
    return [vs.as_value(a) for a in vs.visit_rows(db, job.id)]


@router.get("/jobs/{job_id}/visits", response_model=None)
def list_visits(
    job_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    # Gated as GET /api/jobs/{job_id} is: anyone who can open the job.
    _ = user
    return _visit_list(db, _load_job(db, job_id))


class DayRange(BaseModel):
    start: date = Field(alias="from")
    end: date = Field(alias="to")
    skip_weekends: bool = False


class AddVisitsBody(BaseModel):
    days: list[date] | None = Field(default=None, max_length=vs.MAX_DAYS)
    range: DayRange | None = None
    start_time: time
    duration_minutes: int | None = Field(default=None, ge=15, le=24 * 60)
    tech_ids: list[str] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _one_of(self) -> AddVisitsBody:
        if (self.days is None) == (self.range is None):
            raise ValueError("send either days or range")
        return self


@router.post(
    "/jobs/{job_id}/visits",
    response_model=None,
    status_code=201,
    dependencies=[Depends(require_permission("jobs.write"))],
)
def add_visits(
    job_id: str,
    payload: AddVisitsBody,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    job = _writable_job(db, user, job_id)
    if payload.range is not None:
        try:
            days = vs.expand_range(payload.range.start, payload.range.end, skip_weekends=payload.range.skip_weekends)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    else:
        days = list(payload.days or [])
    tz_name = vs.shop_tz(db)
    techs = payload.tech_ids if payload.tech_ids is not None else vs.job_crew(db, job)
    if payload.tech_ids is not None:
        _check_techs(db, payload.tech_ids)
    plan = vs.plan_add_visits(
        _values(db, job), tz_name=tz_name, today=shop_today(tz_name), job_state=vs._job_state(job),
        days=days, start_time=payload.start_time,
        duration_minutes=payload.duration_minutes or vs.default_duration(db, job),
        tech_ids=[str(t) for t in techs], fields=vs.job_visit_fields(db, job),
    )
    if plan.refusal is not None:
        return _refused(plan.refusal)
    return _apply(db, job, plan, user, "visits_booked")


class MoveVisitBody(BaseModel):
    # Shop-local, as Add day(s) takes them: the server owns the zone.
    day: date
    start_time: time
    duration_minutes: int = Field(ge=15, le=24 * 60)
    # Absent: the tech stays. null: the visit becomes an unassigned slot.
    tech_id: str | None = Field(default=None, max_length=36)


@router.patch(
    "/jobs/{job_id}/visits/{visit_id}",
    response_model=None,
    dependencies=[Depends(require_permission("jobs.write"))],
)
def move_visit(
    job_id: str,
    visit_id: str,
    payload: MoveVisitBody,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    job = _writable_job(db, user, job_id)
    if "tech_id" in payload.model_fields_set:
        _check_techs(db, [payload.tech_id])
    tz_name = vs.shop_tz(db)
    start_at = vs.shop_instant(payload.day, payload.start_time, tz_name)
    plan = vs.plan_move_visit(
        _values(db, job), tz_name=tz_name, today=shop_today(tz_name), job_state=vs._job_state(job),
        visit_id=_parse_visit_id(visit_id), start_at=start_at,
        end_at=start_at + timedelta(minutes=payload.duration_minutes),
        tech_id=payload.tech_id if "tech_id" in payload.model_fields_set else vs.UNSET,
    )
    if plan.refusal is not None:
        return _refused(plan.refusal)
    return _apply(db, job, plan, user, "visit_edited")


@router.delete(
    "/jobs/{job_id}/visits/{visit_id}",
    response_model=None,
    dependencies=[Depends(require_permission("jobs.write"))],
)
def remove_visit(
    job_id: str,
    visit_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    job = _writable_job(db, user, job_id)
    plan = vs.plan_remove_visit(
        _values(db, job), tz_name=vs.shop_tz(db), job_state=vs._job_state(job),
        visit_id=_parse_visit_id(visit_id),
    )
    if plan.refusal is not None:
        return _refused(plan.refusal)
    return _apply(db, job, plan, user, "visit_removed")
