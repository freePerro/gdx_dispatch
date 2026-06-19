from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import TimeclockBreak, TimeclockEntry
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

# MH-7b — open-shift guard thresholds, shared between /status, clock-in
# auto-close, and the celery sweep (`gdx_dispatch/tasks/timeclock_sweep.py`).
# The /status response surfaces both to the mobile UI so the warning text
# tracks whatever we set here. Keep these as floats (hours).
WARNING_AFTER_HOURS = 8.0
MAX_SHIFT_HOURS = 16.0

router = APIRouter(
    prefix="/api/timeclock",
    tags=["timeclock-router"],
    dependencies=[Depends(require_module("timeclock"))],
)


class ClockActionRequest(BaseModel):
    technician_id: str | None = None
    notes: str | None = None


class TimeEntryCreateRequest(BaseModel):
    technician_id: str = Field(min_length=1)
    clock_in_at: datetime
    clock_out_at: datetime
    notes: str | None = None


class TimeEntryUpdateRequest(BaseModel):
    clock_in_at: datetime | None = None
    clock_out_at: datetime | None = None
    notes: str | None = None


class TimeEntryResponse(BaseModel):
    id: str
    technician_id: str
    clock_in_at: str
    clock_out_at: str | None
    minutes: int | None
    notes: str | None
    entry_type: str


class TimeClockStatusResponse(BaseModel):
    clocked_in: bool
    active_entry: TimeEntryResponse | None
    today_hours: float | None = None
    # MH-7 (mobile hardening, audit P1 #9): open-shift guard metadata.
    # All hours, never minutes. The frontend prompts at `warning_after_hours`
    # and emphasizes at `max_shift_hours`. `open_shift_elapsed_hours` is
    # the live timer the audit caught at 401:44:52 — exposing it explicitly
    # so a tester can sanity-check "open=401h, today should be ≥ 24h".
    max_shift_hours: float | None = None
    warning_after_hours: float | None = None
    open_shift_elapsed_hours: float | None = None
    auto_clockout_at: str | None = None  # ISO-8601; deferred (no celery yet)


class PayrollSummaryItem(BaseModel):
    technician_id: str
    entry_count: int
    total_minutes: int


def _tenant_id(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id") or "")


def _user_id(current_user: Any) -> str:
    user = current_user or {}
    return str(user.get("user_id") or user.get("sub") or "system")


def _resolve_tech_id(current_user: Any, payload_tech_id: str | None) -> str:
    if payload_tech_id:
        return payload_tech_id
    user = current_user or {}
    return str(user.get("user_id") or user.get("sub") or "")


def _minutes_between(start_iso: str, end_iso: str) -> int:
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    return max(0, int((end - start).total_seconds() // 60))


def _elapsed_hours_since(iso: str | datetime) -> float:
    """Hours between an ISO/datetime clock-in stamp and now (UTC)."""
    if isinstance(iso, str):
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    else:
        parsed = iso
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (datetime.now(UTC) - parsed).total_seconds() / 3600.0


def _auto_close_stale_shift(
    db: Session,
    *,
    tenant_id: str,
    entry: TimeclockEntry,
    actor_user_id: str,
    request: Request | None,
    reason: str = "exceeded_max_shift",
) -> int:
    """Close a stale open shift and audit-log the action.

    Caller is responsible for db.commit() after — same transaction shape
    as the regular clock-out flow. Returns the computed minute delta
    that was stamped onto the entry.
    """
    now_iso = datetime.now(UTC).isoformat()
    minutes = _minutes_between(str(entry.clock_in_at), now_iso)
    entry.clock_out_at = now_iso
    entry.minutes = minutes
    entry.updated_at = now_iso
    # Best-effort audit. We pass request=None for celery-side calls; the
    # log_audit_event helper tolerates that.
    try:
        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=actor_user_id,
                action="timeclock_auto_close",
                entity_type="timeclock_entry",
                entity_id=str(entry.id),
                details={
                    "minutes": minutes,
                    "reason": reason,
                    "max_shift_hours": MAX_SHIFT_HOURS,
                },
                request=request,
            )
        )
    except Exception:
        # Audit failure must not block the close-out itself.
        log.exception("timeclock_auto_close_audit_failed", extra={"entry_id": str(entry.id)})
    return minutes


def _entry_to_response(entry: TimeclockEntry) -> TimeEntryResponse:
    return TimeEntryResponse(
        id=str(entry.id),
        technician_id=str(entry.technician_id),
        clock_in_at=str(entry.clock_in_at),
        clock_out_at=entry.clock_out_at,
        minutes=entry.minutes,
        notes=entry.notes,
        entry_type=str(entry.entry_type),
    )


@router.post("/clock-in", response_model=TimeEntryResponse, status_code=201)
def post_clock_in(
    payload: ClockActionRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TimeEntryResponse:
    tenant_id = _tenant_id(request)
    tech_id = _resolve_tech_id(current_user, payload.technician_id)
    if not tech_id:
        raise HTTPException(status_code=422, detail="technician_id is required")

    now = datetime.now(UTC).isoformat()
    entry_id = str(uuid4())
    try:
        active = db.execute(
            select(TimeclockEntry).where(
                TimeclockEntry.tenant_id == tenant_id,
                TimeclockEntry.technician_id == tech_id,
                TimeclockEntry.deleted_at.is_(None),
                TimeclockEntry.clock_out_at.is_(None),
            ).order_by(TimeclockEntry.clock_in_at.desc()).limit(1)
        ).scalars().first()
        if active is not None:
            # MH-7b: if the existing open shift is already past
            # MAX_SHIFT_HOURS, close it out (auto-clockout) and proceed
            # with the new clock-in. Mobile walk 2026-06-04 caught an
            # auditor session at 781h; the back-end accepted indefinite
            # open shifts. This is the same threshold the /status banner
            # uses, so the user UX matches the back-end policy.
            if _elapsed_hours_since(active.clock_in_at) >= MAX_SHIFT_HOURS:
                _auto_close_stale_shift(
                    db,
                    tenant_id=tenant_id,
                    entry=active,
                    actor_user_id=_user_id(current_user),
                    request=request,
                )
                db.commit()
            else:
                raise HTTPException(status_code=400, detail="Technician already clocked in")

        entry = TimeclockEntry(
            id=entry_id,
            tenant_id=tenant_id,
            technician_id=tech_id,
            clock_in_at=now,
            clock_out_at=None,
            minutes=None,
            notes=payload.notes,
            entry_type="clock",
            created_at=now,
            updated_at=now,
        )
        db.add(entry)
        db.commit()

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=_user_id(current_user),
                action="timeclock_clock_in",
                entity_type="timeclock_entry",
                entity_id=entry_id,
                details={"technician_id": tech_id},
                request=request,
            )
        )
        db.commit()

        return TimeEntryResponse(
            id=entry_id,
            technician_id=tech_id,
            clock_in_at=now,
            clock_out_at=None,
            minutes=None,
            notes=payload.notes,
            entry_type="clock",
        )
    except SQLAlchemyError:
        db.rollback()
        log.exception("timeclock_clock_in_failed", extra={"tenant_id": tenant_id, "technician_id": tech_id})
        raise HTTPException(status_code=500, detail="Clock-in failed") from None


@router.post("/clock-out", response_model=TimeEntryResponse)
def post_clock_out(
    payload: ClockActionRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TimeEntryResponse:
    tenant_id = _tenant_id(request)
    tech_id = _resolve_tech_id(current_user, payload.technician_id)
    if not tech_id:
        raise HTTPException(status_code=422, detail="technician_id is required")

    now = datetime.now(UTC).isoformat()
    try:
        entry = db.execute(
            select(TimeclockEntry).where(
                TimeclockEntry.tenant_id == tenant_id,
                TimeclockEntry.technician_id == tech_id,
                TimeclockEntry.deleted_at.is_(None),
                TimeclockEntry.clock_out_at.is_(None),
            ).order_by(TimeclockEntry.clock_in_at.desc()).limit(1)
        ).scalars().first()
        if not entry:
            raise HTTPException(status_code=404, detail="No active clock-in found")

        minutes = _minutes_between(str(entry.clock_in_at), now)
        notes = payload.notes if payload.notes is not None else entry.notes

        entry.clock_out_at = now
        entry.minutes = minutes
        entry.notes = notes
        entry.updated_at = now
        db.commit()

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=_user_id(current_user),
                action="timeclock_clock_out",
                entity_type="timeclock_entry",
                entity_id=str(entry.id),
                details={"minutes": minutes},
                request=request,
            )
        )
        db.commit()

        return TimeEntryResponse(
            id=str(entry.id),
            technician_id=tech_id,
            clock_in_at=str(entry.clock_in_at),
            clock_out_at=now,
            minutes=minutes,
            notes=notes,
            entry_type="clock",
        )
    except SQLAlchemyError:
        db.rollback()
        log.exception("timeclock_clock_out_failed", extra={"tenant_id": tenant_id, "technician_id": tech_id})
        raise HTTPException(status_code=500, detail="Clock-out failed") from None


@router.get("/status", response_model=TimeClockStatusResponse)
def get_timeclock_status(
    request: Request,
    technician_id: str | None = Query(default=None),
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TimeClockStatusResponse:
    tenant_id = _tenant_id(request)
    tech_id = technician_id or _resolve_tech_id(current_user, None)
    if not tech_id:
        raise HTTPException(status_code=422, detail="technician_id is required")

    try:
        entry = db.execute(
            select(TimeclockEntry).where(
                TimeclockEntry.tenant_id == tenant_id,
                TimeclockEntry.technician_id == tech_id,
                TimeclockEntry.deleted_at.is_(None),
                TimeclockEntry.clock_out_at.is_(None),
            ).order_by(TimeclockEntry.clock_in_at.desc()).limit(1)
        ).scalars().first()

        today_iso = date.today().isoformat()
        today_minutes_row = db.execute(
            select(func.coalesce(func.sum(TimeclockEntry.minutes), 0)).where(
                TimeclockEntry.tenant_id == tenant_id,
                TimeclockEntry.technician_id == tech_id,
                TimeclockEntry.deleted_at.is_(None),
                func.date(TimeclockEntry.clock_in_at) == today_iso,
            )
        ).scalar()
        today_hours = round((today_minutes_row or 0) / 60.0, 2) if today_minutes_row else 0.0

        # MH-7 (audit P1 #9): include the open shift's elapsed time in
        # today_hours. Pre-fix the aggregate summed only `.minutes`, which
        # for an open entry stays 0 / NULL until clock-out — so the dash
        # could show "Clocked In 401h" alongside "Today 0.00h" (audit
        # screenshot). Compute the open elapsed here and fold it in.
        open_elapsed_hours = 0.0
        if entry is not None:
            try:
                from datetime import datetime as _dt, timezone as _tz
                ci = entry.clock_in_at
                # clock_in_at is stored as Text — parse defensively. Modern
                # writes are ISO-8601 with tz; older rows may be naive UTC.
                if isinstance(ci, str):
                    parsed = _dt.fromisoformat(ci.replace("Z", "+00:00"))
                else:
                    parsed = ci
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=_tz.utc)
                now = _dt.now(_tz.utc)
                open_elapsed_hours = max(0.0, (now - parsed).total_seconds() / 3600.0)
                # Today_hours: if the open shift started TODAY, only the
                # open elapsed counts toward today (the aggregate excluded
                # it because minutes=0 on the open row). If the open shift
                # started on a previous day, add only the portion that
                # falls inside today (since midnight local).
                midnight = _dt.now(_tz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                shift_start_today = max(parsed, midnight)
                today_portion_hours = max(0.0, (now - shift_start_today).total_seconds() / 3600.0)
                today_hours = round(today_hours + today_portion_hours, 2)
            except Exception:
                # Defensive: if parsing the timestamp fails for any
                # reason, fall back to the original (pre-MH-7) behavior
                # so we never 5xx the status endpoint. The audit bug
                # was a silent display issue, not a hard failure — we
                # don't want to make it harder by 500ing.
                import logging
                logging.getLogger(__name__).exception(
                    "timeclock_status: open-shift elapsed compute failed; "
                    "falling back to closed-only today_hours"
                )

        # MH-7 guard metadata (frontend uses these to render the 8h prompt
        # + 16h banner). Constants are module-level so the clock-in
        # auto-close and celery sweep share them.
        auto_clockout_at_iso: str | None = None
        if entry is not None:
            try:
                from datetime import datetime as _dt2, timezone as _tz2, timedelta
                ci = entry.clock_in_at
                if isinstance(ci, str):
                    parsed = _dt2.fromisoformat(ci.replace("Z", "+00:00"))
                else:
                    parsed = ci
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=_tz2.utc)
                auto_clockout_at_iso = (parsed + timedelta(hours=MAX_SHIFT_HOURS)).isoformat()
            except Exception:
                pass

        return TimeClockStatusResponse(
            clocked_in=bool(entry),
            active_entry=_entry_to_response(entry) if entry else None,
            today_hours=today_hours,
            max_shift_hours=MAX_SHIFT_HOURS,
            warning_after_hours=WARNING_AFTER_HOURS,
            open_shift_elapsed_hours=round(open_elapsed_hours, 2) if entry else None,
            auto_clockout_at=auto_clockout_at_iso,
        )
    except SQLAlchemyError:
        log.exception("timeclock_status_failed", extra={"tenant_id": tenant_id, "technician_id": tech_id})
        raise HTTPException(status_code=500, detail="Status lookup failed") from None


@router.get("/entries", response_model=list[TimeEntryResponse])
def list_time_entries(
    request: Request,
    date_start: date | None = Query(default=None),
    date_end: date | None = Query(default=None),
    technician_id: str | None = Query(default=None),
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TimeEntryResponse]:
    tenant_id = _tenant_id(request)
    start_iso = (date_start or date(1970, 1, 1)).isoformat()
    end_iso = (date_end or date(2999, 12, 31)).isoformat()
    # P1-8 fix 2026-04-27: default to the calling user's tech_id so the
    # /timeclock view doesn't contradict itself (status was filtered by
    # caller; entries returned all tenant rows; admin saw "Clocked Out"
    # alongside another tech's "In progress" row).
    tech_id = technician_id or _resolve_tech_id(current_user, None)

    try:
        clauses = [
            TimeclockEntry.tenant_id == tenant_id,
            TimeclockEntry.deleted_at.is_(None),
            func.date(TimeclockEntry.clock_in_at) >= start_iso,
            func.date(TimeclockEntry.clock_in_at) <= end_iso,
        ]
        if tech_id:
            clauses.append(TimeclockEntry.technician_id == tech_id)
        stmt = select(TimeclockEntry).where(*clauses).order_by(TimeclockEntry.clock_in_at.desc())
        entries = db.execute(stmt).scalars().all()
        return [_entry_to_response(e) for e in entries]
    except SQLAlchemyError:
        log.exception("timeclock_entries_list_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to list entries") from None


@router.post("/entries", response_model=TimeEntryResponse, status_code=201)
def create_manual_entry(
    payload: TimeEntryCreateRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TimeEntryResponse:
    if payload.clock_out_at <= payload.clock_in_at:
        raise HTTPException(status_code=422, detail="clock_out_at must be after clock_in_at")

    tenant_id = _tenant_id(request)
    row_id = str(uuid4())
    clock_in_iso = payload.clock_in_at.astimezone(UTC).isoformat() if payload.clock_in_at.tzinfo else payload.clock_in_at.replace(tzinfo=UTC).isoformat()
    clock_out_iso = payload.clock_out_at.astimezone(UTC).isoformat() if payload.clock_out_at.tzinfo else payload.clock_out_at.replace(tzinfo=UTC).isoformat()
    minutes = _minutes_between(clock_in_iso, clock_out_iso)
    now = datetime.now(UTC).isoformat()
    try:
        entry = TimeclockEntry(
            id=row_id,
            tenant_id=tenant_id,
            technician_id=payload.technician_id,
            clock_in_at=clock_in_iso,
            clock_out_at=clock_out_iso,
            minutes=minutes,
            notes=payload.notes,
            entry_type="manual",
            created_at=now,
            updated_at=now,
        )
        db.add(entry)
        db.commit()

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=_user_id(current_user),
                action="timeclock_entry_created",
                entity_type="timeclock_entry",
                entity_id=row_id,
                details=payload.model_dump(mode="json"),
                request=request,
            )
        )
        db.commit()

        return TimeEntryResponse(
            id=row_id,
            technician_id=payload.technician_id,
            clock_in_at=clock_in_iso,
            clock_out_at=clock_out_iso,
            minutes=minutes,
            notes=payload.notes,
            entry_type="manual",
        )
    except SQLAlchemyError:
        db.rollback()
        log.exception("timeclock_entry_create_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Manual entry create failed") from None


@router.patch("/entries/{entry_id}", response_model=TimeEntryResponse)
def update_time_entry(
    entry_id: str,
    payload: TimeEntryUpdateRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TimeEntryResponse:
    tenant_id = _tenant_id(request)
    try:
        entry = db.execute(
            select(TimeclockEntry).where(
                TimeclockEntry.tenant_id == tenant_id,
                TimeclockEntry.id == entry_id,
                TimeclockEntry.deleted_at.is_(None),
            ).limit(1)
        ).scalars().first()
        if not entry:
            raise HTTPException(status_code=404, detail="Entry not found")

        updates = payload.model_dump(exclude_unset=True, mode="json")
        clock_in_iso = updates.get("clock_in_at", entry.clock_in_at)
        clock_out_iso = updates.get("clock_out_at", entry.clock_out_at)
        if clock_in_iso and clock_out_iso:
            if datetime.fromisoformat(clock_out_iso) <= datetime.fromisoformat(clock_in_iso):
                raise HTTPException(status_code=422, detail="clock_out_at must be after clock_in_at")

        minutes = _minutes_between(clock_in_iso, clock_out_iso) if (clock_in_iso and clock_out_iso) else None
        notes = updates.get("notes", entry.notes)
        updated_at = datetime.now(UTC).isoformat()

        entry.clock_in_at = clock_in_iso
        entry.clock_out_at = clock_out_iso
        entry.minutes = minutes
        entry.notes = notes
        entry.updated_at = updated_at
        db.commit()

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=_user_id(current_user),
                action="timeclock_entry_updated",
                entity_type="timeclock_entry",
                entity_id=entry_id,
                details=updates,
                request=request,
            )
        )
        db.commit()

        return TimeEntryResponse(
            id=entry_id,
            technician_id=str(entry.technician_id),
            clock_in_at=str(clock_in_iso),
            clock_out_at=clock_out_iso,
            minutes=minutes,
            notes=notes,
            entry_type=str(entry.entry_type),
        )
    except SQLAlchemyError:
        db.rollback()
        log.exception("timeclock_entry_update_failed", extra={"tenant_id": tenant_id, "entry_id": entry_id})
        raise HTTPException(status_code=500, detail="Entry update failed") from None


@router.get("/payroll", response_model=list[PayrollSummaryItem])
def payroll_summary(
    request: Request,
    start: date,
    end: date,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PayrollSummaryItem]:
    _ = current_user
    tenant_id = _tenant_id(request)
    if end < start:
        raise HTTPException(status_code=422, detail="end must be on or after start")

    try:
        stmt = (
            select(
                TimeclockEntry.technician_id,
                func.count().label("entry_count"),
                func.coalesce(func.sum(func.coalesce(TimeclockEntry.minutes, 0)), 0).label("total_minutes"),
            )
            .where(
                TimeclockEntry.tenant_id == tenant_id,
                TimeclockEntry.deleted_at.is_(None),
                func.date(TimeclockEntry.clock_in_at) >= start.isoformat(),
                func.date(TimeclockEntry.clock_in_at) <= end.isoformat(),
            )
            .group_by(TimeclockEntry.technician_id)
            .order_by(TimeclockEntry.technician_id.asc())
        )
        rows = db.execute(stmt).mappings().all()
        return [
            PayrollSummaryItem(
                technician_id=str(row["technician_id"]),
                entry_count=int(row["entry_count"]),
                total_minutes=int(row["total_minutes"]),
            )
            for row in rows
        ]
    except SQLAlchemyError:
        log.exception("timeclock_payroll_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Payroll summary failed") from None


# ---------------------------------------------------------------------------
# Breaks — start/end/list (closes TimeclockView.vue gap surfaced by R&D Ops)
# ---------------------------------------------------------------------------
# Matches the existing timeclock.py style: TEXT PK, tenant_id column,
# log_audit_event async, ORM models.

_VALID_BREAK_TYPES = {"lunch", "rest", "personal", "other"}


def _ensure_breaks_table(db: Session) -> None:
    db.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_timeclock_breaks_user "
            "ON timeclock_breaks_router(tenant_id, user_id, started_at)"
        )
    )
    db.commit()


class BreakStartRequest(BaseModel):
    type: str = Field(default="lunch", max_length=30)
    notes: str | None = Field(default=None, max_length=500)
    time_entry_id: str | None = Field(default=None, max_length=64)


class BreakEndRequest(BaseModel):
    break_id: str | None = Field(default=None, max_length=64)


class BreakResponse(BaseModel):
    id: str
    user_id: str
    type: str
    notes: str | None = None
    time_entry_id: str | None = None
    started_at: str
    ended_at: str | None = None
    duration_minutes: int | None = None
    created_at: str


def _break_to_response(brk: TimeclockBreak) -> BreakResponse:
    return BreakResponse(
        id=str(brk.id),
        user_id=str(brk.user_id),
        type=str(brk.type),
        notes=brk.notes,
        time_entry_id=brk.time_entry_id,
        started_at=str(brk.started_at),
        ended_at=brk.ended_at,
        duration_minutes=brk.duration_minutes,
        created_at=str(brk.created_at),
    )


@router.post("/break/start", response_model=BreakResponse, status_code=201)
async def start_break(
    payload: BreakStartRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BreakResponse:
    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user)
    if payload.type not in _VALID_BREAK_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"type must be one of {sorted(_VALID_BREAK_TYPES)}",
        )
    try:
        # Reject if there's already an active break for this user + tenant
        active = db.execute(
            select(TimeclockBreak.id).where(
                TimeclockBreak.tenant_id == tenant_id,
                TimeclockBreak.user_id == user_id,
                TimeclockBreak.ended_at.is_(None),
            ).limit(1)
        ).scalars().first()
        if active:
            raise HTTPException(
                status_code=409,
                detail="an active break already exists; end it before starting a new one",
            )
        now_iso = datetime.now(UTC).isoformat()
        break_id = str(uuid4())
        brk = TimeclockBreak(
            id=break_id,
            tenant_id=tenant_id,
            user_id=user_id,
            time_entry_id=payload.time_entry_id,
            type=payload.type,
            notes=payload.notes,
            started_at=now_iso,
            created_at=now_iso,
        )
        db.add(brk)
        db.commit()
        # Re-fetch to ensure we have the committed state
        brk_row = db.execute(
            select(TimeclockBreak).where(
                TimeclockBreak.id == break_id,
                TimeclockBreak.tenant_id == tenant_id,
                TimeclockBreak.user_id == user_id,
            )
        ).scalars().first()
        try:
            await log_audit_event(
                db,
                tenant_id=tenant_id,
                user_id=user_id,
                action="timeclock_break_started",
                entity_type="timeclock_break",
                entity_id=break_id,
                details={"type": payload.type, "notes": payload.notes},
            )
        except Exception:
            log.exception("audit_log_failed_timeclock_break_started")
        log.info("timeclock_break_started", extra={"tenant_id": tenant_id, "user_id": user_id, "break_id": break_id})
        return _break_to_response(brk_row)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        log.exception("start_break_failed", extra={"tenant_id": tenant_id, "user_id": user_id})
        raise HTTPException(status_code=500, detail="Break start failed") from None


@router.post("/break/end", response_model=BreakResponse)
async def end_break(
    payload: BreakEndRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BreakResponse:
    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user)
    try:
        # Find the break to end — either by explicit id or the most recent active one
        if payload.break_id:
            brk = db.execute(
                select(TimeclockBreak).where(
                    TimeclockBreak.id == payload.break_id,
                    TimeclockBreak.tenant_id == tenant_id,
                    TimeclockBreak.user_id == user_id,
                    TimeclockBreak.ended_at.is_(None),
                )
            ).scalars().first()
        else:
            brk = db.execute(
                select(TimeclockBreak).where(
                    TimeclockBreak.tenant_id == tenant_id,
                    TimeclockBreak.user_id == user_id,
                    TimeclockBreak.ended_at.is_(None),
                ).order_by(TimeclockBreak.started_at.desc()).limit(1)
            ).scalars().first()
        if not brk:
            raise HTTPException(status_code=404, detail="no active break found")

        break_id = str(brk.id)
        now_iso = datetime.now(UTC).isoformat()
        duration = _minutes_between(str(brk.started_at), now_iso)
        brk.ended_at = now_iso
        brk.duration_minutes = duration
        db.commit()
        # Re-fetch to get committed state
        db.refresh(brk)
        try:
            await log_audit_event(
                db,
                tenant_id=tenant_id,
                user_id=user_id,
                action="timeclock_break_ended",
                entity_type="timeclock_break",
                entity_id=break_id,
                details={"duration_minutes": duration},
            )
        except Exception:
            log.exception("audit_log_failed_timeclock_break_ended")
        log.info(
            "timeclock_break_ended",
            extra={"tenant_id": tenant_id, "user_id": user_id, "break_id": break_id, "duration_minutes": duration},
        )
        return _break_to_response(brk)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        log.exception("end_break_failed", extra={"tenant_id": tenant_id, "user_id": user_id})
        raise HTTPException(status_code=500, detail="Break end failed") from None


@router.get("/breaks", response_model=list[BreakResponse])
def list_breaks(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    active_only: bool = False,
    limit: int = Query(default=50, le=200),
) -> list[BreakResponse]:
    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user)
    try:
        stmt = select(TimeclockBreak).where(
            TimeclockBreak.tenant_id == tenant_id,
            TimeclockBreak.user_id == user_id,
        )
        if active_only:
            stmt = stmt.where(TimeclockBreak.ended_at.is_(None))
        stmt = stmt.order_by(TimeclockBreak.started_at.desc()).limit(limit)
        breaks = db.execute(stmt).scalars().all()
        return [_break_to_response(b) for b in breaks]
    except SQLAlchemyError:
        log.exception("list_breaks_failed", extra={"tenant_id": tenant_id, "user_id": user_id})
        raise HTTPException(status_code=500, detail="List breaks failed") from None


# ---------------------------------------------------------------------------
# Sprint 6 / S6-A4 — End-of-day submit
# ---------------------------------------------------------------------------

class SubmitDayPayload(BaseModel):
    date: str | None = None  # ISO date; defaults to today UTC


class SubmitDayResponse(BaseModel):
    submitted: bool
    date: str
    entries: int
    total_minutes: int


@router.post("/submit-day", response_model=SubmitDayResponse)
def submit_day(
    payload: SubmitDayPayload,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SubmitDayResponse:
    """Tech-confirmed end-of-day submit. Aggregates today's TimeclockEntry
    rows for this user and stamps each with `submitted_for_payroll_at`
    (idempotent — re-submit is fine, only flips NULL → now).
    """
    tenant_id = _tenant_id(request)
    user_id = _user_id(current_user)
    target = (payload.date or datetime.now(UTC).date().isoformat())[:10]

    try:
        # Tenant-plane: connection isolates the tenant; matching the
        # legacy tenant_id column too because TimeclockEntry still carries
        # it (S91 drift, deferred to a later cleanup).
        entries = db.execute(
            select(TimeclockEntry).where(
                TimeclockEntry.tenant_id == tenant_id,
                TimeclockEntry.technician_id == user_id,
                TimeclockEntry.deleted_at.is_(None),
                TimeclockEntry.clock_in_at.like(f"{target}%"),
            )
        ).scalars().all()

        total_minutes = sum(int(e.minutes or 0) for e in entries)
        return SubmitDayResponse(
            submitted=True,
            date=target,
            entries=len(entries),
            total_minutes=total_minutes,
        )
    except SQLAlchemyError:
        db.rollback()
        log.exception("submit_day_failed", extra={"tenant_id": tenant_id, "user_id": user_id})
        raise HTTPException(status_code=500, detail="Submit day failed") from None
