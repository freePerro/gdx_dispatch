"""Time off requests, the office's direct time-off entries, and holiday pay.

Sits beside routers/timeclock.py under the same module gate. A tech asks for
paid days off here; the office approves, denies, or records time off outright;
the holiday calendar (Settings) is posted to the timesheet from here. Every
mutation names its actor in the audit log, and approval stages the timeclock
entries, the request update and the audit row in ONE transaction — the
entries never exist without their trail.

Who may do what is a `require_role` dependency on each route (not an inline
check) so the authz permission ratchet counts these as authorized rather
than growing its baseline:

  * request / cancel — any signed-in role except viewer, on their OWN
    record; dispatch managers may name someone else.
  * approve / deny / revoke / direct entry / post holiday — dispatch managers
    (owner, admin, dispatcher, manager), the same tier that reads the crew
    timesheet.

The hours rules live in core/time_off.py; nothing here invents a stamp.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import ensure_audit_table, log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.core.pay_periods import shop_today
from gdx_dispatch.core.permissions import is_dispatch_manager
from gdx_dispatch.core.roles import (
    ACCOUNTING,
    ADMIN,
    DISPATCH_MANAGER_ROLES,
    DISPATCHER,
    MANAGER,
    OWNER,
    SALES,
    TECHNICIAN,
)
from gdx_dispatch.core.time_off import (
    DEFAULT_MINUTES,
    MAX_LOOKAHEAD_DAYS,
    MAX_MINUTES_PER_DAY,
    MAX_REQUEST_DAYS,
    MIN_MINUTES_PER_DAY,
    REQUESTABLE_TYPES,
    STATUS_APPROVED,
    STATUS_CANCELLED,
    STATUS_DENIED,
    STATUS_PENDING,
    STATUS_REVOKED,
    STATUSES,
    TIME_OFF_TYPES,
    create_time_off_entries,
    holiday_on,
    holidays_between,
    is_time_off,
    person_schedule,
    posted_holiday_counts,
    user_names,
    workdays_between,
)
from gdx_dispatch.models.tenant_models import AppSettings, TimeclockEntry, TimeOffRequest
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

# How far back a person may ask for their own time off — the same window the
# self-service shift correction uses (routers/timeclock.py), for the same
# reason: older weeks are paid weeks and the office owns corrections there.
SELF_SERVICE_BACKDATE_DAYS = 14

router = APIRouter(
    prefix="/api/timeclock/time-off",
    tags=["time-off"],
    dependencies=[Depends(require_module("timeclock"))],
)

# Every builtin role except viewer: a viewer holds no payroll record of their
# own to request against.
_can_request = require_role(OWNER, ADMIN, DISPATCHER, MANAGER, TECHNICIAN, SALES, ACCOUNTING)
_manager = require_role(*sorted(DISPATCH_MANAGER_ROLES))


# ── Schemas ──────────────────────────────────────────────────────────────────


class TimeOffRequestIn(BaseModel):
    # Omitted means "me". A manager may name someone else (recording a
    # request phoned in); anyone else naming another person is refused.
    technician_id: str | None = Field(default=None, max_length=36)
    entry_type: str = "vacation"
    start_date: date
    end_date: date
    minutes_per_day: int | None = Field(default=None, ge=MIN_MINUTES_PER_DAY, le=MAX_MINUTES_PER_DAY)
    notes: str | None = Field(default=None, max_length=1000)


class ReviewIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class TimeOffEntriesIn(BaseModel):
    technician_id: str = Field(min_length=1, max_length=36)
    entry_type: str
    start_date: date
    end_date: date
    minutes_per_day: int | None = Field(default=None, ge=MIN_MINUTES_PER_DAY, le=MAX_MINUTES_PER_DAY)
    notes: str | None = Field(default=None, max_length=1000)


class HolidayPostIn(BaseModel):
    date: date
    technician_ids: list[str] = Field(min_length=1, max_length=500)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _tenant_id(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id") or "")


def _user_id(current_user: Any) -> str:
    user = current_user or {}
    return str(user.get("user_id") or user.get("sub") or "system")


def _settings(db: Session) -> AppSettings | None:
    try:
        return db.query(AppSettings).first()
    except SQLAlchemyError:
        log.exception("time_off_settings_read_failed")
        return None


def _tz(settings: AppSettings | None) -> str:
    return getattr(settings, "timezone", None) or "America/New_York"


def _default_minutes(settings: AppSettings | None) -> int:
    value = getattr(settings, "time_off_default_minutes", None)
    return int(value) if value else DEFAULT_MINUTES


def _audit(
    db: Session,
    request: Request,
    current_user: Any,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, Any],
) -> None:
    log_audit_event_sync(
        db,
        tenant_id=_tenant_id(request),
        user_id=_user_id(current_user),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
        request=request,
    )


def _serialize(row: TimeOffRequest, names: dict[str, str], workday_count: int | None = None) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "technician_id": str(row.technician_id),
        "technician_name": names.get(str(row.technician_id)),
        "entry_type": row.entry_type,
        "entry_type_label": TIME_OFF_TYPES.get(row.entry_type, row.entry_type),
        "start_date": row.start_date.isoformat(),
        "end_date": row.end_date.isoformat(),
        "minutes_per_day": int(row.minutes_per_day),
        "notes": row.notes,
        "status": row.status,
        "requested_by": row.requested_by,
        "requested_by_name": names.get(str(row.requested_by)),
        "reviewed_by": row.reviewed_by,
        "reviewed_by_name": names.get(str(row.reviewed_by)) if row.reviewed_by else None,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "review_note": row.review_note,
        "entry_ids": list(row.entry_ids or []),
        "workday_count": workday_count,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _load_request(db: Session, request_id: str) -> TimeOffRequest:
    row = db.execute(
        select(TimeOffRequest).where(
            TimeOffRequest.id == str(request_id),
            TimeOffRequest.deleted_at.is_(None),
        ).limit(1)
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Request not found")
    return row


def _transition(db: Session, row: TimeOffRequest, *, from_status: str, to_status: str, now: datetime) -> None:
    """Move the request from one status to another, or refuse with 409.

    A conditional UPDATE, not a read-then-write: two managers pressing
    Approve in the same second both read `pending`, and without this the
    second would create a second set of entries. The database decides who
    was first; the loser gets the same 409 a stale screen gets.
    """
    result = db.execute(
        sa_update(TimeOffRequest)
        .where(
            TimeOffRequest.id == str(row.id),
            TimeOffRequest.status == from_status,
            TimeOffRequest.deleted_at.is_(None),
        )
        .values(status=to_status, updated_at=now)
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"request is no longer {from_status}")
    row.status = to_status
    row.updated_at = now


def _validate_range(start: date, end: date) -> None:
    if end < start:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date")
    if (end - start).days + 1 > MAX_REQUEST_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"a single request covers at most {MAX_REQUEST_DAYS} days",
        )


def _overlapping_request(db: Session, technician_id: str, start: date, end: date) -> TimeOffRequest | None:
    return db.execute(
        select(TimeOffRequest).where(
            TimeOffRequest.technician_id == str(technician_id),
            TimeOffRequest.deleted_at.is_(None),
            TimeOffRequest.status.in_([STATUS_PENDING, STATUS_APPROVED]),
            TimeOffRequest.start_date <= end,
            TimeOffRequest.end_date >= start,
        ).limit(1)
    ).scalars().first()


# ── Options ──────────────────────────────────────────────────────────────────


@router.get("/options", response_model=None)
def time_off_options(
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """What a request form needs to know, for anyone signed in. Techs cannot
    read /api/settings (admin only), and the form must not guess the day
    length: a guess that disagrees with the server's cap is a 422 the tech
    cannot understand. The overtime statement is policy, not a secret, and
    the Timesheets page shows it beside the time-off hours."""
    settings = _settings(db)
    workdays = getattr(settings, "default_workdays", None)
    return {
        "types": dict(TIME_OFF_TYPES),
        "requestable_types": list(REQUESTABLE_TYPES),
        "default_minutes": _default_minutes(settings),
        "min_minutes_per_day": MIN_MINUTES_PER_DAY,
        "max_request_days": MAX_REQUEST_DAYS,
        "self_service_backdate_days": SELF_SERVICE_BACKDATE_DAYS,
        "workdays": 31 if workdays is None else int(workdays),
        "time_off_counts_toward_overtime": bool(
            getattr(settings, "time_off_counts_toward_overtime", False)
        ),
        "timezone": _tz(settings),
    }


# ── Requests ─────────────────────────────────────────────────────────────────


@router.get("/requests", response_model=None)
def list_requests(
    request: Request,
    status: str | None = Query(default=None),
    all_technicians: bool = Query(
        default=False,
        description="Office view: every person's requests. Dispatch/admin only.",
    ),
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    if all_technicians and not is_dispatch_manager(current_user):
        raise HTTPException(status_code=403, detail="dispatcher or admin role required")
    if status and status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of: {', '.join(STATUSES)}")
    clauses = [TimeOffRequest.deleted_at.is_(None)]
    if not all_technicians:
        clauses.append(TimeOffRequest.technician_id == _user_id(current_user))
    if status:
        clauses.append(TimeOffRequest.status == status)
    try:
        rows = db.execute(
            select(TimeOffRequest).where(*clauses)
            .order_by(TimeOffRequest.created_at.desc())
            .limit(500)
        ).scalars().all()
    except SQLAlchemyError:
        log.exception("time_off_list_failed")
        raise HTTPException(status_code=500, detail="Failed to list requests") from None
    ids = {str(r.technician_id) for r in rows} | {str(r.requested_by) for r in rows}
    ids |= {str(r.reviewed_by) for r in rows if r.reviewed_by}
    names = user_names(db, ids)
    settings = _settings(db)
    out = []
    for r in rows:
        schedule = person_schedule(db, settings, str(r.technician_id))
        out.append(_serialize(r, names, len(workdays_between(r.start_date, r.end_date, schedule.workdays))))
    return out


@router.post("/requests", response_model=None, status_code=201, dependencies=[Depends(_can_request)])
def create_request(
    payload: TimeOffRequestIn,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    own = _user_id(current_user)
    manager = is_dispatch_manager(current_user)
    tech_id = (payload.technician_id or "").strip() or own
    if tech_id != own and not manager:
        raise HTTPException(status_code=403, detail="cannot request time off for another person")

    kind = str(payload.entry_type or "").strip().lower()
    if kind not in REQUESTABLE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"entry_type must be one of: {', '.join(REQUESTABLE_TYPES)}",
        )
    _validate_range(payload.start_date, payload.end_date)

    settings = _settings(db)
    tz_name = _tz(settings)
    today = shop_today(tz_name)
    default_minutes = _default_minutes(settings)
    minutes = int(payload.minutes_per_day or default_minutes)
    notes = (payload.notes or "").strip() or None

    if not manager:
        # The same guardrails as a self-added shift: say why, stay inside the
        # window the office has not paid yet, and never exceed a day.
        if not notes:
            raise HTTPException(status_code=422, detail="a note saying why is required")
        if payload.start_date < today - timedelta(days=SELF_SERVICE_BACKDATE_DAYS):
            raise HTTPException(
                status_code=422,
                detail=f"days older than {SELF_SERVICE_BACKDATE_DAYS} days are entered by the office",
            )
        if minutes > default_minutes:
            raise HTTPException(
                status_code=422,
                detail=f"a day off is at most {default_minutes / 60:g} hours here",
            )
    if payload.start_date > today + timedelta(days=MAX_LOOKAHEAD_DAYS):
        raise HTTPException(status_code=422, detail="that is more than a year away — check the date")

    schedule = person_schedule(db, settings, tech_id)
    days = workdays_between(payload.start_date, payload.end_date, schedule.workdays)
    if not days:
        raise HTTPException(status_code=422, detail="no workdays fall in that range")

    clash = _overlapping_request(db, tech_id, payload.start_date, payload.end_date)
    if clash is not None:
        raise HTTPException(
            status_code=409,
            detail=f"a {clash.status} request already covers {clash.start_date.isoformat()} to {clash.end_date.isoformat()}",
        )

    row = TimeOffRequest(
        id=str(uuid4()),
        company_id=_tenant_id(request),
        technician_id=tech_id,
        entry_type=kind,
        start_date=payload.start_date,
        end_date=payload.end_date,
        minutes_per_day=minutes,
        notes=notes,
        status=STATUS_PENDING,
        requested_by=own,
    )
    try:
        ensure_audit_table(db)
        db.add(row)
        _audit(
            db, request, current_user,
            action="time_off_requested", entity_type="time_off_request", entity_id=row.id,
            details={
                "technician_id": tech_id, "entry_type": kind,
                "start_date": payload.start_date.isoformat(), "end_date": payload.end_date.isoformat(),
                "minutes_per_day": minutes, "workdays": len(days),
            },
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("time_off_request_failed")
        raise HTTPException(status_code=500, detail="Request could not be saved") from None
    db.refresh(row)
    return _serialize(row, user_names(db, {tech_id, own}), len(days))


@router.post("/requests/{request_id}/approve", response_model=None, dependencies=[Depends(_manager)])
def approve_request(
    request_id: str,
    payload: ReviewIn | None = None,
    request: Request = None,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = _load_request(db, request_id)
    if row.status != STATUS_PENDING:
        raise HTTPException(status_code=409, detail=f"request is already {row.status}")
    settings = _settings(db)
    schedule = person_schedule(db, settings, str(row.technician_id))
    days = workdays_between(row.start_date, row.end_date, schedule.workdays)
    if not days:
        raise HTTPException(status_code=422, detail="no workdays fall in that range any more — deny it instead")
    now = datetime.now(UTC)
    try:
        ensure_audit_table(db)
        # Claim the request FIRST: if another manager got here a moment ago
        # this raises 409 before any entry is staged.
        _transition(db, row, from_status=STATUS_PENDING, to_status=STATUS_APPROVED, now=now)
        made = create_time_off_entries(
            db,
            tenant_id=_tenant_id(request),
            technician_id=str(row.technician_id),
            entry_type=row.entry_type,
            days=days,
            minutes=int(row.minutes_per_day),
            notes=row.notes,
            settings=settings,
        )
        row.reviewed_by = _user_id(current_user)
        row.reviewed_at = now
        row.review_note = ((payload.note if payload else None) or "").strip() or None
        row.entry_ids = made.entry_ids
        _audit(
            db, request, current_user,
            action="time_off_approved", entity_type="time_off_request", entity_id=row.id,
            details={
                "technician_id": str(row.technician_id), "entry_type": row.entry_type,
                "start_date": row.start_date.isoformat(), "end_date": row.end_date.isoformat(),
                "minutes_per_day": int(row.minutes_per_day),
                "entry_ids": made.entry_ids,
                "skipped_days": [d.isoformat() for d in made.skipped],
            },
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("time_off_approve_failed", extra={"request_id": request_id})
        raise HTTPException(status_code=500, detail="Approval could not be saved") from None
    db.refresh(row)
    out = _serialize(row, user_names(db, {str(row.technician_id), row.reviewed_by or ""}), len(days))
    out["created"] = len(made.created)
    out["skipped_days"] = [d.isoformat() for d in made.skipped]
    return out


@router.post("/requests/{request_id}/deny", response_model=None, dependencies=[Depends(_manager)])
def deny_request(
    request_id: str,
    payload: ReviewIn | None = None,
    request: Request = None,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = _load_request(db, request_id)
    if row.status != STATUS_PENDING:
        raise HTTPException(status_code=409, detail=f"request is already {row.status}")
    now = datetime.now(UTC)
    try:
        ensure_audit_table(db)
        _transition(db, row, from_status=STATUS_PENDING, to_status=STATUS_DENIED, now=now)
        row.reviewed_by = _user_id(current_user)
        row.reviewed_at = now
        row.review_note = ((payload.note if payload else None) or "").strip() or None
        _audit(
            db, request, current_user,
            action="time_off_denied", entity_type="time_off_request", entity_id=row.id,
            details={"technician_id": str(row.technician_id), "note": row.review_note},
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("time_off_deny_failed", extra={"request_id": request_id})
        raise HTTPException(status_code=500, detail="Denial could not be saved") from None
    db.refresh(row)
    return _serialize(row, user_names(db, {str(row.technician_id), row.reviewed_by or ""}))


@router.post("/requests/{request_id}/cancel", response_model=None, dependencies=[Depends(_can_request)])
def cancel_request(
    request_id: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = _load_request(db, request_id)
    own = _user_id(current_user)
    if str(row.technician_id) != own and not is_dispatch_manager(current_user):
        raise HTTPException(status_code=403, detail="cannot cancel another person's request")
    if row.status != STATUS_PENDING:
        raise HTTPException(status_code=409, detail=f"request is already {row.status}")
    now = datetime.now(UTC)
    try:
        ensure_audit_table(db)
        _transition(db, row, from_status=STATUS_PENDING, to_status=STATUS_CANCELLED, now=now)
        _audit(
            db, request, current_user,
            action="time_off_cancelled", entity_type="time_off_request", entity_id=row.id,
            details={"technician_id": str(row.technician_id)},
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("time_off_cancel_failed", extra={"request_id": request_id})
        raise HTTPException(status_code=500, detail="Cancel could not be saved") from None
    db.refresh(row)
    return _serialize(row, user_names(db, {str(row.technician_id)}))


@router.post("/requests/{request_id}/revoke", response_model=None, dependencies=[Depends(_manager)])
def revoke_request(
    request_id: str,
    payload: ReviewIn | None = None,
    request: Request = None,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Undo an approval: the entries it created are soft-deleted, by id, so a
    worked shift on the same day is never touched."""
    row = _load_request(db, request_id)
    if row.status != STATUS_APPROVED:
        raise HTTPException(status_code=409, detail=f"only an approved request can be revoked (this one is {row.status})")
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    removed: list[str] = []
    try:
        ensure_audit_table(db)
        _transition(db, row, from_status=STATUS_APPROVED, to_status=STATUS_REVOKED, now=now)
        ids = [str(i) for i in (row.entry_ids or [])]
        if ids:
            entries = db.execute(
                select(TimeclockEntry).where(
                    TimeclockEntry.id.in_(ids),
                    TimeclockEntry.deleted_at.is_(None),
                )
            ).scalars().all()
            for entry in entries:
                if not is_time_off(entry.entry_type):
                    continue  # never delete a worked shift, whatever the list says
                entry.deleted_at = now_iso
                entry.updated_at = now_iso
                removed.append(str(entry.id))
        row.reviewed_by = _user_id(current_user)
        row.reviewed_at = now
        row.review_note = ((payload.note if payload else None) or "").strip() or None
        _audit(
            db, request, current_user,
            action="time_off_revoked", entity_type="time_off_request", entity_id=row.id,
            details={"technician_id": str(row.technician_id), "entry_ids_removed": removed, "note": row.review_note},
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("time_off_revoke_failed", extra={"request_id": request_id})
        raise HTTPException(status_code=500, detail="Revoke could not be saved") from None
    db.refresh(row)
    out = _serialize(row, user_names(db, {str(row.technician_id), row.reviewed_by or ""}))
    out["removed"] = len(removed)
    return out


# ── Office direct entry ──────────────────────────────────────────────────────


@router.post("/entries", response_model=None, status_code=201, dependencies=[Depends(_manager)])
def create_entries(
    payload: TimeOffEntriesIn,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Record time off without a request — a tech phoned it in, or the office
    is catching up. Same workday expansion and idempotency as an approval."""
    kind = str(payload.entry_type or "").strip().lower()
    if kind not in TIME_OFF_TYPES:
        raise HTTPException(status_code=422, detail=f"entry_type must be one of: {', '.join(TIME_OFF_TYPES)}")
    _validate_range(payload.start_date, payload.end_date)
    settings = _settings(db)
    minutes = int(payload.minutes_per_day or _default_minutes(settings))
    schedule = person_schedule(db, settings, payload.technician_id)
    days = workdays_between(payload.start_date, payload.end_date, schedule.workdays)
    if not days:
        raise HTTPException(status_code=422, detail="no workdays fall in that range")
    try:
        ensure_audit_table(db)
        made = create_time_off_entries(
            db,
            tenant_id=_tenant_id(request),
            technician_id=payload.technician_id,
            entry_type=kind,
            days=days,
            minutes=minutes,
            notes=payload.notes,
            settings=settings,
        )
        _audit(
            db, request, current_user,
            action="time_off_entries_created", entity_type="timeclock_entry",
            entity_id=made.entry_ids[0] if made.entry_ids else "none",
            details={
                "technician_id": payload.technician_id, "entry_type": kind,
                "start_date": payload.start_date.isoformat(), "end_date": payload.end_date.isoformat(),
                "minutes_per_day": minutes, "entry_ids": made.entry_ids,
                "skipped_days": [d.isoformat() for d in made.skipped],
            },
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("time_off_entries_failed")
        raise HTTPException(status_code=500, detail="Time off could not be saved") from None
    return {
        "created": len(made.created),
        "entry_ids": made.entry_ids,
        "skipped_days": [d.isoformat() for d in made.skipped],
    }


# ── Holidays ─────────────────────────────────────────────────────────────────


@router.get("/holidays", response_model=None)
def list_holidays(
    request: Request,
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """The calendar in a range, each with how many people already hold the
    day. Defaults to the shop's current calendar year. Open to any signed-in
    user: dates and names, no hours of anyone's."""
    settings = _settings(db)
    tz_name = _tz(settings)
    today = shop_today(tz_name)
    lo = start or date(today.year, 1, 1)
    hi = end or date(today.year, 12, 31)
    if hi < lo:
        raise HTTPException(status_code=422, detail="end must be on or after start")
    rows = holidays_between(settings, lo, hi)
    days = [date.fromisoformat(h["date"]) for h in rows]
    try:
        posted = posted_holiday_counts(db, days, tz_name)
    except SQLAlchemyError:
        log.exception("holiday_posted_counts_failed")
        posted = {}
    return [
        {**h, "posted": int(posted.get(date.fromisoformat(h["date"]), 0))}
        for h in rows
    ]


@router.post("/holidays/post", response_model=None, dependencies=[Depends(_manager)])
def post_holiday(
    payload: HolidayPostIn,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Give the named people the calendar's paid hours for that day. Anyone
    who already holds a time-off entry on the day is skipped and named, so
    posting twice pays nobody twice."""
    settings = _settings(db)
    holiday = holiday_on(settings, payload.date)
    if holiday is None:
        raise HTTPException(
            status_code=422,
            detail=f"{payload.date.isoformat()} is not on the holiday calendar — add it in Settings first",
        )
    people = []
    for raw in payload.technician_ids:
        tid = str(raw or "").strip()
        if tid and tid not in people:
            people.append(tid)
    if not people:
        raise HTTPException(status_code=422, detail="choose at least one person")
    created: dict[str, list[str]] = {}
    skipped: list[str] = []
    try:
        ensure_audit_table(db)
        for tid in people:
            made = create_time_off_entries(
                db,
                tenant_id=_tenant_id(request),
                technician_id=tid,
                entry_type="holiday",
                days=[payload.date],
                minutes=int(holiday["minutes"]),
                notes=holiday["name"],
                settings=settings,
            )
            if made.created:
                created[tid] = made.entry_ids
            else:
                skipped.append(tid)
        _audit(
            db, request, current_user,
            action="holiday_posted", entity_type="holiday", entity_id=payload.date.isoformat(),
            details={
                "name": holiday["name"], "minutes": int(holiday["minutes"]),
                "created": created, "skipped": skipped,
            },
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("holiday_post_failed")
        raise HTTPException(status_code=500, detail="Holiday could not be posted") from None
    return {
        "date": payload.date.isoformat(),
        "name": holiday["name"],
        "minutes": int(holiday["minutes"]),
        "created": len(created),
        "skipped": skipped,
        "entry_ids": [i for ids in created.values() for i in ids],
    }
