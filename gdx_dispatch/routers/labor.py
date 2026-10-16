"""Job labor: the per-job hours in `time_entries`.

Two clocks, two tables, two purposes (Doug, 2026-10-15). The time clock a
tech punches for PAYROLL is `timeclock_entries_router` (routers/timeclock.py,
TimesheetsView). JOB TRACKING, meaning how long was spent on which job, is
`time_entries`: written here, by mobile arrival and by closeout and day close,
and read by job costing, the tech's invoicing screen and labor billing. This
router never touches the payroll clock, and nothing written here is meant to
change a paycheck. Do not join or reconcile the two.

What keeps that true in code today is `user_id`, not the table: rows written
here carry `tech_id` and leave `user_id` NULL, and routers/payroll.py
`_fetch_tech_hours` still sums `time_entries` grouped by `user_id` (people-
time's code, which contradicts the rule above and is theirs to retire). So a
row with a `user_id` is the tech's own time and is never editable here
(_refuse_if_locked).
"""
from __future__ import annotations

import contextlib
import logging
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, resolve_audit_actor
from gdx_dispatch.core.database import contained_read, get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.pay_periods import resolve_zone, shop_today_from_settings, shop_tz_name_from_settings
from gdx_dispatch.core.permissions import is_dispatch_manager
from gdx_dispatch.models.tenant_models import Job, Technician, TimeEntry

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
# Plan §3: the DISPLAY cost fallback when a labor row stored no hourly_rate.
# Prod's pricing_settings.loaded_labor_cost_per_hour is $65 (the real
# wage-plus-burden number) — the one correctly-configured cost rate, which
# nothing read. This is that fallback; the stored rate still wins when set
# (the standing "never re-resolve a written rate" rule).
DEFAULT_COST_RATE = 65.0


def _cost_rate_fallback(db: Session) -> float:
    # KNOWN LIMIT (M28 review): the model documents loaded_labor_cost_per_hour
    # = 0 as "labor is pure profit" — a deliberate value — but the column's
    # server_default is ALSO 0, so a deliberate zero and "never configured"
    # are indistinguishable in data. `> 0` therefore reads a deliberate 0 as
    # unconfigured and returns $65 — the same species of bug M28 kills, one
    # level up. Unrepresentable without a NULL-default migration; filed as a
    # follow-up rather than bundled. Inert on prod today (value is 65.00).
    try:
        from gdx_dispatch.models.pricing_engine import PricingSettings
        row = db.execute(select(PricingSettings).limit(1)).scalar_one_or_none()
        if row is not None and row.loaded_labor_cost_per_hour is not None:
            v = float(row.loaded_labor_cost_per_hour)
            if v > 0:
                return v
    except SQLAlchemyError:
        log.exception("cost_rate_fallback_read_failed")
        # M28 review: a failed SELECT poisons a PG transaction; without this
        # the NEXT query in the caller 500s and the graceful degradation the
        # callers promise survives only when they happen to catch-all.
        with contextlib.suppress(SQLAlchemyError):
            db.rollback()
    return DEFAULT_COST_RATE
# No entry_type on either model: rows written here are always office-entered
# ("manual"). A caller that could set "job" would mint a tech timer day close
# attests and billing claims (services/day_close.is_candidate), or a row
# _refuse_if_locked then refuses to edit. extra="forbid" answers 422 to a
# request that still sends it, rather than dropping it silently.
class TimeEntryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tech_id: str = Field(min_length=1)
    clock_in: datetime
    clock_out: datetime


class TimeEntryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tech_id: str | None = None
    clock_in: datetime | None = None
    clock_out: datetime | None = None


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
    """The tech's stored rate, or DEFAULT_HOURLY_RATE when there is no row.

    ``contained_read`` (GDXA-158). This decides the stored COST rate on a
    ``TimeEntry`` from a swallowed read, and both write callers reach it with
    work already pending: ``create_job_time_entry`` calls it inside the
    ``TimeEntry(...)`` constructor, and ``update_time_entry`` calls it one line
    after mutating ``row.tech_id``. On Postgres a bare swallow aborted the
    transaction, so the caller's ``commit()`` died with 25P02 and the tech's
    entry was not written. Containing it trades that loud, retryable 500 for a
    committed row costed at DEFAULT_HOURLY_RATE ($50), which a stored rate then
    keeps forever (``_labor_rate_for``) and which only the log line marks as
    defaulted — chosen because losing the tech's hours is the worse half. Reads
    only, so the savepoint sits on the Connection (core.database rules 2/3), and
    it goes INSIDE the ``try`` so the degraded default still happens (rule 1).
    Insurance on a real mechanism, not a fix for an observed outage — see
    ``contained_read``'s docstring for how rarely prod, as configured, can
    fail this read.

    ``jobs.py::_labor_rate_for`` wraps THIS function and deliberately does not
    add a savepoint of its own: rule 5 — a callee that swallows its own failure
    exits the block clean, and the RELEASE SAVEPOINT would raise 25P02 out of
    the ``with`` itself. Containing it here is what fixes that caller too.
    """
    try:
        with contained_read(db):
            tech = db.execute(
                select(Technician).where(Technician.id == tech_id).limit(1)
            ).scalar_one_or_none()
    except SQLAlchemyError:
        log.exception("resolve_hourly_rate_failed", extra={"tech_id": tech_id})
        tech = None
    if not tech:
        return DEFAULT_HOURLY_RATE
    return DEFAULT_HOURLY_RATE if tech.hourly_rate is None else float(tech.hourly_rate)


def _entry_cost(entry: TimeEntry, fallback_rate: float = DEFAULT_COST_RATE) -> float:
    minutes = entry.duration_minutes or 0
    # Stored rate wins — INCLUDING a deliberate $0 (warranty/comp/no-charge).
    # `x or fallback` treats 0.0 as falsy and would silently re-rate a $0 row
    # to $65 (audit round 2); the fallback applies ONLY when no rate is stored.
    # Read the column directly (not _to_float, which collapses None→0.0 and
    # would make an ABSENT rate indistinguishable from a stored $0).
    stored = entry.hourly_rate
    rate = fallback_rate if stored is None else float(stored)
    return round((minutes / 60.0) * rate, 2)


def _entry_to_dict(
    entry: TimeEntry, *, name: str | None = None, fallback_rate: float = DEFAULT_COST_RATE,
) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "job_id": str(entry.job_id),
        "tech_id": entry.tech_id,
        # A row with a user_id is the tech's own time; the job page locks it.
        "user_id": entry.user_id or None,
        # Plan §3: the Tech column was blank because this key never existed.
        "tech_name": name or entry.tech_name or None,
        "clock_in": entry.clock_in.isoformat() if entry.clock_in else None,
        "clock_out": entry.clock_out.isoformat() if entry.clock_out else None,
        "duration_minutes": entry.duration_minutes,
        "entry_type": entry.entry_type,
        "hourly_rate": _to_float(entry.hourly_rate),
        "labor_cost": _entry_cost(entry, fallback_rate),
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        "deleted_at": entry.deleted_at.isoformat() if entry.deleted_at else None,
        # GDXA-1065: the job page's edit/delete actions lock a billed,
        # day-closed or timer row and say why; the server refuses it anyway.
        "day_closed_at": entry.day_closed_at.isoformat() if entry.day_closed_at else None,
        "billed_invoice_id": str(entry.billed_invoice_id) if entry.billed_invoice_id else None,
    }


def _audit_fields(entry: TimeEntry) -> dict[str, Any]:
    """What an audit row records about a time entry: the fields a write can
    change, so a before/after pair answers "what changed" (GDXA-1065)."""
    return {
        "tech_id": entry.tech_id,
        "clock_in": entry.clock_in.isoformat() if entry.clock_in else None,
        "clock_out": entry.clock_out.isoformat() if entry.clock_out else None,
        "duration_minutes": entry.duration_minutes,
        "entry_type": entry.entry_type,
        "hourly_rate": _to_float(entry.hourly_rate) if entry.hourly_rate is not None else None,
    }


def _refuse_if_locked(row: TimeEntry) -> None:
    """A day row an invoice has claimed is that invoice's labor evidence
    (core/closeout_billing.py). Editing or deleting it would leave the invoice
    billing hours the record no longer shows, so refuse until the invoice
    releases it (void, delete, or removing its attested labor line).

    A day-closed row is refused too, billed or not: day close attests it, and
    billing reads exactly those rows (day_row_clauses). Editing one would
    change attested hours, and giving minutes to the 0-minute timer day close
    shut unvouched would bill labor already recorded on that day's own row.

    Every other row that is not office-entered ("manual", the only type this
    router writes) is refused as well. A job timer belongs to the tech's clock
    and day close (services/day_close.is_candidate reads exactly these rows):
    giving it a clock_out or minutes here makes that tech's day close refuse
    them as having no open time. A final closeout with no arrival timer writes
    its attested row as "work" (routers/jobs.py, CLOSEOUT_LABOR_NOTE): that
    day bills from the closeout record, so an edit here would split job
    costing from the invoice, and the next re-closeout rewrites the row in
    place anyway. The column DEFAULTS to "manual" (tenant_models), so the type
    alone fails open for a writer that omits it, or an old row whose type was
    rewritten: a row carrying a user_id is a tech's own time (payroll's summary
    and jobs.py's user-less-row rule both key on it), and this router never
    sets one, so that row is refused whatever its type. Day close and the
    invoice claim stamp only non-manual rows, so the billed and day-closed
    checks are a backstop and no check-then-commit race remains to lock
    against."""
    if row.billed_invoice_id is not None:
        raise HTTPException(
            status_code=409,
            detail="This time entry is billed on an invoice; void or edit the invoice first.",
        )
    if row.day_closed_at is not None:
        raise HTTPException(
            status_code=409,
            detail="This time entry was closed by day close; its hours are that day's attested labor.",
        )
    if row.entry_type != "manual" or row.user_id:
        raise HTTPException(
            status_code=409,
            detail="This is the tech's own time (clock or closeout); correct it through their closeout, not here.",
        )


def _get_job_or_404(db: Session, job_id: UUID) -> Job:
    row = db.get(Job, job_id)
    if not row or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


@router.get("/jobs/{job_id}/time-entries", response_model=None)
def list_job_time_entries(
    job_id: UUID,
    current_user: dict[str, Any] = Depends(_require_dispatch),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    _get_job_or_404(db, job_id)
    rows = (
        db.query(TimeEntry)
        .filter(TimeEntry.job_id == job_id, TimeEntry.deleted_at.is_(None))
        .order_by(TimeEntry.clock_in.desc())
        .all()
    )
    fallback = _cost_rate_fallback(db)
    names = _resolve_entry_names(db, rows)
    return [
        _entry_to_dict(r, name=names.get(str(r.id)), fallback_rate=fallback)
        for r in rows
    ]


def _resolve_entry_names(db: Session, rows: list[TimeEntry]) -> dict[str, str]:
    """entry.id → display name. A labor row identifies its tech by user_id
    (mobile arrival) or tech_id (which holds a technicians.id OR a users.id).
    Resolve both, prefer a real name, fall back to the stored tech_name."""
    ids: set[str] = set()
    for r in rows:
        for v in (r.user_id, r.tech_id, r.technician_id):
            if v:
                ids.add(str(v))
    if not ids:
        return {str(r.id): (r.tech_name or "") for r in rows if r.tech_name}
    from gdx_dispatch.models.tenant_models import User
    user_names: dict[str, str] = {}
    tech_names: dict[str, str] = {}
    try:
        for u in db.execute(select(User).where(User.id.in_([_maybe_uuid(i) for i in ids if _maybe_uuid(i)]))).scalars():
            user_names[str(u.id).replace("-", "")] = (u.full_name or u.name or u.username or u.email or "")
    except SQLAlchemyError:
        db.rollback()
    try:
        for t in db.execute(select(Technician).where(Technician.id.in_(list(ids)))).scalars():
            if t.name:
                tech_names[str(t.id).replace("-", "")] = t.name  # normalized key
                if t.user_id:
                    user_names.setdefault(str(t.user_id).replace("-", ""), t.name)
    except SQLAlchemyError:
        db.rollback()
    out: dict[str, str] = {}
    for r in rows:
        nm = None
        for v in (r.user_id, r.tech_id, r.technician_id):
            if not v:
                continue
            key = str(v).replace("-", "")
            nm = user_names.get(key) or tech_names.get(key)
            if nm:
                break
        out[str(r.id)] = nm or (r.tech_name or "")
    return out


def _maybe_uuid(v: str):
    try:
        return UUID(str(v))
    except (ValueError, AttributeError):
        return None


@router.post("/jobs/{job_id}/time-entries", response_model=None, status_code=201)
def create_job_time_entry(
    request: Request,
    job_id: UUID,
    payload: TimeEntryCreate,
    current_user: dict[str, Any] = Depends(_require_dispatch),
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
        entry_type="manual",
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
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="create_job_time_entry",
                entity_type="job_time_entry",
                entity_id=str(job_id),
                details={"time_entry_id": str(row.id), "after": _audit_fields(row)},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('create_job_time_entry_audit_failed')
    # M28: the tenant-aware fallback, not the static constant — a no-rate
    # row displayed a different cost here than on the list endpoint.
    return _entry_to_dict(row, fallback_rate=_cost_rate_fallback(db))


@router.patch("/time-entries/{entry_id}", response_model=None)
def update_time_entry(
    entry_id: UUID,
    payload: TimeEntryPatch,
    current_user: dict[str, Any] = Depends(_require_dispatch),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.get(TimeEntry, entry_id)
    if not row or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Time entry not found")
    _refuse_if_locked(row)
    before = _audit_fields(row)

    updates = payload.model_dump(exclude_unset=True)
    if "clock_in" in updates and updates["clock_in"] is None:
        raise HTTPException(status_code=422, detail="clock_in cannot be empty")
    if "clock_out" in updates and updates["clock_out"] is None:
        # An open row is a running timer; office-entered hours always close.
        raise HTTPException(status_code=422, detail="clock_out cannot be empty")
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
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="update_time_entry",
                entity_type="time_entry",
                entity_id=str(entry_id),
                details={"job_id": str(row.job_id), "before": before, "after": _audit_fields(row)},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('update_time_entry_audit_failed')
    # M28: the tenant-aware fallback, not the static constant — a no-rate
    # row displayed a different cost here than on the list endpoint.
    return _entry_to_dict(row, fallback_rate=_cost_rate_fallback(db))


@router.delete("/time-entries/{entry_id}", response_model=None)
def delete_time_entry(
    entry_id: UUID,
    current_user: dict[str, Any] = Depends(_require_dispatch),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    row = db.get(TimeEntry, entry_id)
    if not row or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Time entry not found")
    _refuse_if_locked(row)
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
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="delete_time_entry",
                entity_type="time_entry",
                entity_id=str(entry_id),
                details={"job_id": str(row.job_id), "before": _audit_fields(row)},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('delete_time_entry_audit_failed')
    return {"deleted": True}


@router.get("/reports/labor-summary", response_model=None)
def labor_summary(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    current_user: dict[str, Any] = Depends(_require_dispatch),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    # The window is in shop days: "today" is the shop's, and each day runs
    # shop midnight to shop midnight, so an evening shift is not filed under
    # tomorrow's UTC date (GDXA-509). Bounds go to the query as UTC instants.
    zone = resolve_zone(shop_tz_name_from_settings(db))
    resolved_end = end_date or shop_today_from_settings(db)
    resolved_start = start_date or (resolved_end - timedelta(days=29))
    if resolved_start > resolved_end:
        raise HTTPException(status_code=422, detail="start_date must be on or before end_date")

    start_dt = datetime.combine(resolved_start, time.min, tzinfo=zone).astimezone(UTC)
    end_dt_exclusive = datetime.combine(resolved_end + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)

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
    # M28 review: resolved ONCE — the per-row call ran one
    # pricing_settings SELECT per entry in the profitability path.
    _summary_fb = _cost_rate_fallback(db)
    for row in rows:
        tech_id = row.tech_id
        minutes = float(row.duration_minutes or 0)
        cost = _entry_cost(row, _summary_fb)
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
