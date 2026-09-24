"""Paid time off as timeclock entries — the one place the rules live.

A vacation day or a paid holiday is a CLOSED `TimeclockEntry` whose
`entry_type` is one of `TIME_OFF_TYPES`. Its `clock_in_at` is the person's
shift start on that shop-local day, its `clock_out_at` is that plus the paid
minutes, and `minutes` is the paid minutes. Nothing about it is open, so
`/status`, the stale-shift sweep and the export's `open_shift` flag all read
it as what it is: a finished, paid span.

Why a closed span and not NULL stamps: every reader of the timeclock table
treats `clock_out_at IS NULL` as "still clocked in". A day off with no
clock-out would sit on the mobile status card as a 400-hour shift.

What a reader of paid time must still do differently:

  * break attribution (`core/timesheet_hours.break_minutes_by_entry`) skips
    these entries, or a lunch taken on a day that carries both a worked
    shift and a posted holiday can land on the holiday row;
  * the pay-period export splits worked hours from time-off hours and states
    whether time off counts toward overtime (a per-company setting — the app
    computes no overtime itself — the 2026-09-23 time-off and holiday pay plan).

Two callers create these rows — approving a request and the office's direct
entry — through `create_time_off_entries`, which is idempotent per
(person, shop day): a day that already carries a time-off entry is skipped
and reported, never doubled.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.pay_periods import resolve_zone, shop_day_of
from gdx_dispatch.models.tenant_models import AppSettings, TimeclockEntry, User

log = logging.getLogger(__name__)

#: entry_type → label. Adding a type is one line here plus a frontend label.
TIME_OFF_TYPES: dict[str, str] = {
    "vacation": "Vacation",
    "holiday": "Holiday",
}
#: What a tech may ask for. Holidays come from the calendar, never a request.
REQUESTABLE_TYPES: tuple[str, ...] = ("vacation",)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_CANCELLED = "cancelled"
STATUS_REVOKED = "revoked"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_DENIED, STATUS_CANCELLED, STATUS_REVOKED)

DEFAULT_MINUTES = 480
#: Bounds on a single day's paid minutes, request or direct entry. The lower
#: bound stops a zero-hour "day off" that pays nothing and confuses the sheet;
#: the upper matches MAX_SHIFT_HOURS in core/timesheet_hours.
MIN_MINUTES_PER_DAY = 30
MAX_MINUTES_PER_DAY = 16 * 60
#: A request spans at most this many calendar days, inclusive.
MAX_REQUEST_DAYS = 60
#: A request may not start further ahead than this.
MAX_LOOKAHEAD_DAYS = 366

#: Mon=1 … Sun=64, the `AppSettings.default_workdays` bitmask.
WEEKDAY_BITS = (1, 2, 4, 8, 16, 32, 64)
DEFAULT_WORKDAYS = 31  # Mon–Fri
DEFAULT_SHIFT_START = time(8, 0)


def is_time_off(entry_type: Any) -> bool:
    return str(entry_type or "").strip().lower() in TIME_OFF_TYPES


def type_label(entry_type: Any) -> str:
    key = str(entry_type or "").strip().lower()
    return TIME_OFF_TYPES.get(key, key.capitalize() or "Shift")


def workdays_between(start: date, end: date, workdays_mask: int | None) -> list[date]:
    """Every calendar day in [start, end] whose weekday bit is set.

    A mask of 0 or None yields nothing — an empty week is a configuration the
    caller must refuse, not silently pay.
    """
    mask = int(workdays_mask or 0)
    if end < start or mask <= 0:
        return []
    out: list[date] = []
    day = start
    while day <= end:
        if mask & WEEKDAY_BITS[day.weekday()]:
            out.append(day)
        day += timedelta(days=1)
    return out


@dataclass(frozen=True)
class PersonSchedule:
    shift_start: time
    workdays: int


def person_schedule(db: Session, settings: AppSettings | None, user_id: str) -> PersonSchedule:
    """The person's shift start and workday mask: their `users` override when
    set, else the shop default. Mirrors how the dispatch board reads them."""
    shift_start = getattr(settings, "default_shift_start", None) or DEFAULT_SHIFT_START
    workdays = getattr(settings, "default_workdays", None)
    workdays = DEFAULT_WORKDAYS if workdays is None else int(workdays)
    user = _load_user(db, user_id)
    if user is not None:
        if getattr(user, "shift_start", None):
            shift_start = user.shift_start
        if getattr(user, "workdays", None) is not None:
            workdays = int(user.workdays)
    return PersonSchedule(shift_start=shift_start, workdays=workdays)


def _load_user(db: Session, user_id: str) -> User | None:
    """`users.id` is a Uuid column while the timeclock keys on a text user id;
    compare as UUID so the lookup matches on both engines (SQLite stores a
    Uuid dashless — raw text comparison never matches there).

    A database error here is NOT swallowed. This runs inside the approval
    transaction, before the entries are staged, and on Postgres a failed
    statement aborts the whole transaction: catching it would let the caller
    stage its rows into a session that can no longer commit, and the write
    would fail later with a worse message. The caller's SQLAlchemyError
    handler rolls back and says "could not be saved" — the honest outcome.
    """
    from uuid import UUID

    try:
        uid = UUID(str(user_id))
    except (ValueError, TypeError):
        return None
    return db.execute(
        select(User).where(User.id == uid, User.deleted_at.is_(None)).limit(1)
    ).scalars().first()


def user_names(db: Session, user_ids: set[str]) -> dict[str, str]:
    """id → display name for the ids that resolve; the rest stay unnamed."""
    from uuid import UUID

    parsed: dict[UUID, str] = {}
    for raw in user_ids:
        try:
            parsed[UUID(str(raw))] = str(raw)
        except (ValueError, TypeError):
            continue
    if not parsed:
        return {}
    try:
        rows = db.execute(select(User).where(User.id.in_(list(parsed)))).scalars().all()
    except SQLAlchemyError:
        # Cosmetic — ids still resolve to rows. Called only AFTER the write
        # has committed, so rolling back here clears the aborted transaction
        # a failed statement leaves behind on Postgres without losing anything.
        log.exception("time_off_names_failed")
        db.rollback()
        return {}
    out: dict[str, str] = {}
    for u in rows:
        label = u.full_name or u.name or u.username or u.email
        if label:
            out[parsed.get(u.id, str(u.id))] = str(label)
    return out


def time_off_stamps(day: date, shift_start: time, minutes: int, tz_name: Any) -> tuple[str, str]:
    """(clock_in_iso, clock_out_iso) in UTC for `minutes` of paid time
    starting at `shift_start` on shop-local `day`."""
    zone = resolve_zone(tz_name)
    start = datetime.combine(day, shift_start.replace(tzinfo=None), tzinfo=zone)
    end = start + timedelta(minutes=int(minutes))
    return start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()


def existing_time_off_days(
    db: Session, technician_id: str, days: list[date], tz_name: Any
) -> dict[date, TimeclockEntry]:
    """Shop days in `days` that already carry a live time-off entry for this
    person, whichever type. The window is widened a day each side and the
    exact day decided in shop time, because `clock_in_at` is UTC text."""
    if not days:
        return {}
    lo = date.fromordinal(min(days).toordinal() - 1).isoformat()
    hi = date.fromordinal(max(days).toordinal() + 2).isoformat()
    rows = db.execute(
        select(TimeclockEntry).where(
            TimeclockEntry.technician_id == str(technician_id),
            TimeclockEntry.deleted_at.is_(None),
            TimeclockEntry.entry_type.in_(list(TIME_OFF_TYPES)),
            TimeclockEntry.clock_in_at >= lo,
            TimeclockEntry.clock_in_at < hi,
        )
    ).scalars().all()
    wanted = set(days)
    out: dict[date, TimeclockEntry] = {}
    for row in rows:
        day = shop_day_of(row.clock_in_at, tz_name)
        if day in wanted and day not in out:
            out[day] = row
    return out


@dataclass
class CreatedTimeOff:
    created: list[TimeclockEntry] = field(default_factory=list)
    skipped: list[date] = field(default_factory=list)

    @property
    def entry_ids(self) -> list[str]:
        return [str(e.id) for e in self.created]


def create_time_off_entries(
    db: Session,
    *,
    tenant_id: str,
    technician_id: str,
    entry_type: str,
    days: list[date],
    minutes: int,
    notes: str | None,
    settings: AppSettings | None,
) -> CreatedTimeOff:
    """Stage one closed time-off entry per day in `days`, skipping any day the
    person already has one for. Does NOT commit — the caller owns the
    transaction so the audit row lands with the entries or not at all."""
    kind = str(entry_type or "").strip().lower()
    if kind not in TIME_OFF_TYPES:
        raise ValueError(f"not a time-off type: {entry_type!r}")
    tz_name = getattr(settings, "timezone", None) or "America/New_York"
    schedule = person_schedule(db, settings, technician_id)
    taken = existing_time_off_days(db, technician_id, days, tz_name)
    now = datetime.now(UTC).isoformat()
    result = CreatedTimeOff()
    for day in days:
        if day in taken:
            result.skipped.append(day)
            continue
        clock_in, clock_out = time_off_stamps(day, schedule.shift_start, minutes, tz_name)
        entry = TimeclockEntry(
            id=str(uuid4()),
            tenant_id=tenant_id,
            technician_id=str(technician_id),
            clock_in_at=clock_in,
            clock_out_at=clock_out,
            minutes=int(minutes),
            notes=(notes or "").strip() or TIME_OFF_TYPES[kind],
            entry_type=kind,
            created_at=now,
            updated_at=now,
        )
        db.add(entry)
        result.created.append(entry)
    return result


# ── Holiday calendar ─────────────────────────────────────────────────────────


def normalize_holiday_calendar(raw: Any) -> list[dict[str, Any]]:
    """Validate and canonicalise the calendar: sorted by date, one entry per
    date, ISO date strings, trimmed names, minutes inside the day bounds.
    Raises ValueError with a sentence the settings screen can show."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError("holiday_calendar must be a list")
    out: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each holiday needs a date, a name and hours")
        when = item.get("date")
        if isinstance(when, datetime):
            when = when.date()
        if isinstance(when, date):
            day = when
        else:
            try:
                day = date.fromisoformat(str(when or "")[:10])
            except ValueError:
                raise ValueError(f"not a date: {when!r}") from None
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError(f"the holiday on {day.isoformat()} needs a name")
        if len(name) > 100:
            raise ValueError(f"the holiday name on {day.isoformat()} is too long")
        try:
            minutes = int(item.get("minutes", DEFAULT_MINUTES))
        except (TypeError, ValueError):
            raise ValueError(f"hours for {name} must be a number") from None
        if not (MIN_MINUTES_PER_DAY <= minutes <= MAX_MINUTES_PER_DAY):
            raise ValueError(
                f"hours for {name} must be between {MIN_MINUTES_PER_DAY / 60:g} "
                f"and {MAX_MINUTES_PER_DAY // 60}"
            )
        key = day.isoformat()
        if key in out:
            raise ValueError(f"two holidays on {key} — keep one")
        out[key] = {"date": key, "name": name, "minutes": minutes}
    return [out[k] for k in sorted(out)]


def holiday_calendar(settings: AppSettings | None) -> list[dict[str, Any]]:
    """The stored calendar, canonicalised; a corrupt stored value reads as
    empty and is logged rather than taking the timesheet down."""
    raw = getattr(settings, "holiday_calendar", None)
    try:
        return normalize_holiday_calendar(raw)
    except ValueError:
        log.exception("holiday_calendar_unreadable")
        return []


def holidays_between(settings: AppSettings | None, start: date, end: date) -> list[dict[str, Any]]:
    return [
        h for h in holiday_calendar(settings)
        if start.isoformat() <= h["date"] <= end.isoformat()
    ]


def holiday_on(settings: AppSettings | None, day: date) -> dict[str, Any] | None:
    key = day.isoformat()
    for h in holiday_calendar(settings):
        if h["date"] == key:
            return h
    return None


def unposted_holidays(
    db: Session, settings: AppSettings | None, start: date, end: date
) -> list[dict[str, Any]]:
    """Calendar holidays inside [start, end] that nobody has been paid for.

    The pay-period send holds on these (core/timesheet_delivery.py) for the
    same reason it holds on a flagged shift: a file that leaves without the
    holiday reads as complete and under-pays every person on it. The office
    clears the hold by posting the holiday — or by taking it off the calendar
    if the shop worked that day.
    """
    rows = holidays_between(settings, start, end)
    if not rows:
        return []
    tz_name = getattr(settings, "timezone", None) or "America/New_York"
    posted = posted_holiday_counts(db, [date.fromisoformat(h["date"]) for h in rows], tz_name)
    return [h for h in rows if not posted.get(date.fromisoformat(h["date"]), 0)]


def posted_holiday_counts(
    db: Session, days: list[date], tz_name: Any
) -> dict[date, int]:
    """How many people already hold a `holiday` entry on each shop day."""
    if not days:
        return {}
    lo = date.fromordinal(min(days).toordinal() - 1).isoformat()
    hi = date.fromordinal(max(days).toordinal() + 2).isoformat()
    rows = db.execute(
        select(TimeclockEntry.clock_in_at, TimeclockEntry.technician_id).where(
            TimeclockEntry.deleted_at.is_(None),
            TimeclockEntry.entry_type == "holiday",
            TimeclockEntry.clock_in_at >= lo,
            TimeclockEntry.clock_in_at < hi,
        )
    ).all()
    wanted = set(days)
    seen: dict[date, set[str]] = {}
    for clock_in, tech in rows:
        day = shop_day_of(clock_in, tz_name)
        if day in wanted:
            seen.setdefault(day, set()).add(str(tech))
    return {day: len(people) for day, people in seen.items()}
