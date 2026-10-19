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

import calendar
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, true
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import contained_read
from gdx_dispatch.core.pay_periods import resolve_zone, shop_day_of, shop_tz_name
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
#: `AppSettings.default_shift_end` is NOT NULL with this server default; the
#: fallback only covers a database with no settings row at all.
DEFAULT_SHIFT_END = time(17, 0)


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
    shift_end: time = DEFAULT_SHIFT_END

    def works_on(self, day: date) -> bool:
        """Whether ``day``'s weekday bit is set in the person's mask."""
        return bool(int(self.workdays or 0) & WEEKDAY_BITS[day.weekday()])


def person_schedule(db: Session, settings: AppSettings | None, user_id: str) -> PersonSchedule:
    """The person's shift start, shift end and workday mask: their `users`
    override when set, else the shop default, per field. Mirrors how the
    dispatch board reads them (routers/technicians.py `_tech_to_dict`)."""
    return _schedule_for(settings, _load_user(db, user_id))


def person_schedules(
    db: Session, settings: AppSettings | None, user_ids: set[str]
) -> dict[str, PersonSchedule]:
    """`person_schedule` for many people in one `users` query, keyed by the
    text id passed in. Every id gets an entry: one that does not parse or
    names no live user falls back to the shop defaults, exactly as the
    single lookup does. A database error propagates, as in `_load_user`."""
    parsed = _parse_user_ids(user_ids)
    users: dict[UUID, User] = {}
    if parsed:
        users = {
            u.id: u
            for u in db.execute(
                select(User).where(User.id.in_(list(parsed)), User.deleted_at.is_(None))
            ).scalars()
        }
    uuid_of = {raw: uid for uid, raw in parsed.items()}
    return {
        raw: _schedule_for(settings, users.get(uuid_of.get(str(raw))))
        for raw in user_ids
    }


def _schedule_for(settings: AppSettings | None, user: User | None) -> PersonSchedule:
    """The per-field override of the shop default by the user's own values."""
    shift_start = getattr(settings, "default_shift_start", None) or DEFAULT_SHIFT_START
    shift_end = getattr(settings, "default_shift_end", None) or DEFAULT_SHIFT_END
    workdays = getattr(settings, "default_workdays", None)
    workdays = DEFAULT_WORKDAYS if workdays is None else int(workdays)
    if user is not None:
        if getattr(user, "shift_start", None):
            shift_start = user.shift_start
        if getattr(user, "shift_end", None):
            shift_end = user.shift_end
        if getattr(user, "workdays", None) is not None:
            workdays = int(user.workdays)
    return PersonSchedule(shift_start=shift_start, workdays=workdays, shift_end=shift_end)


def shift_end_at(day: date, schedule: PersonSchedule, tz_name: Any) -> datetime:
    """The UTC instant of the person's shift end on shop-local ``day``."""
    end = schedule.shift_end.replace(tzinfo=None)
    return datetime.combine(day, end, tzinfo=resolve_zone(tz_name)).astimezone(UTC)


def shift_end_label(schedule: PersonSchedule) -> str:
    """"4:30 PM" — the wall-clock shift end, as the timesheet prints a time."""
    return schedule.shift_end.strftime("%-I:%M %p")


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


def _parse_user_ids(raw_ids: set[str]) -> dict[UUID, str]:
    """UUID → the text id the timeclock stores, for the ids that parse. A
    `Uuid` column compares as UUID on both engines; raw text never matches
    SQLite's dashless storage."""
    parsed: dict[UUID, str] = {}
    for raw in raw_ids:
        try:
            parsed[UUID(str(raw))] = str(raw)
        except (ValueError, TypeError):
            continue
    return parsed


def user_names(db: Session, user_ids: set[str]) -> dict[str, str]:
    """id → display name for the ids that resolve; the rest stay unnamed."""
    parsed = _parse_user_ids(user_ids)
    if not parsed:
        return {}
    try:
        # SAVEPOINT, replacing a `db.rollback()` in the handler (GDXA-164
        # sibling sweep). Be exact about what the rollback was and was not:
        # FIVE of the six callers reach here just after their own `db.commit()`
        # (`create_request`, `approve_request`, `deny_request`,
        # `cancel_request`, `revoke_request`), so for those there was indeed no
        # pending work to discard. The sixth, `list_requests`, is a pure GET
        # that never commits at all — it just read `rows`, and
        # the rollback expired every one of them, buying a re-SELECT during
        # `_serialize`. Be exact about when that bit: the rollback sat inside
        # the `except`, so the cost landed only on the requests where the name
        # read actually failed — a latent cost on the read path, not one paid
        # on every list. An earlier draft implied the latter.
        # Either way it is the wrong tool per `core.database.contained_read`
        # rule 4 — a full rollback expires everything the caller holds, and it
        # is silently destructive the day a seventh caller arrives with
        # something staged. The savepoint clears the aborted transaction
        # without either cost.
        #
        # One thing the swap gives up, recorded rather than glossed: a
        # `db.rollback()` also RECOVERS a session that was already poisoned
        # upstream, and a savepoint does not — `contained_read.__enter__`
        # raises 25P02 on an aborted transaction and leaves it dead (limit 1 in
        # its docstring, measured again here). No caller can reach this in that
        # state today, since the five writers commit immediately before and
        # `list_requests` reads nothing that could poison first. It is a
        # recovery path removed, not one that was in use.
        with contained_read(db):
            rows = db.execute(select(User).where(User.id.in_(list(parsed)))).scalars().all()
    except SQLAlchemyError:
        # Cosmetic — ids still resolve to rows, they just carry no label.
        log.exception("time_off_names_failed")
        return {}
    out: dict[str, str] = {}
    for u in rows:
        label = u.full_name or u.name or u.username or u.email
        if label:
            out[parsed.get(u.id, str(u.id))] = str(label)
    return out


def user_labels(
    db: Session,
    tenant_id: str,
    ids: set[str] | None = None,
    *,
    active_only: bool = False,
    log_event: str = "user_labels_failed",
) -> dict[str, str]:
    """id → display label for one tenant's users, best effort.

    The one label lookup behind the timesheet exception card and export
    (`routers/timeclock._tech_names`), the roster's active staff and the
    pay-period send (`tasks/payroll_timesheet._names`). Those were three raw
    `id::text = ANY(:ids)` queries that failed on SQLite and were swallowed,
    so no SQLite test could see a name (GDXA-1793).

    `ids=None` means every user of the tenant; an empty set means none. Ids
    compare as UUIDs, never text (SQLite stores a `Uuid` dashless), and each
    key is returned as the caller spelled it. Without `ids` a key is the
    dashed lowercase form, which is what Postgres' `id::text` produced.
    `active_only` drops soft-deleted users and treats a NULL `active` as
    active, as `users._serialize` does.

    Only the label columns are selected, so a `users` table behind the ORM
    elsewhere cannot fail this read. A database error is contained and
    swallowed: a missing label must not cost the caller its session.
    """
    parsed: dict[UUID, str] | None = None
    if ids is not None:
        parsed = _parse_user_ids(ids)
        if not parsed:
            return {}
    stmt = select(User.id, User.full_name, User.name, User.username, User.email).where(
        User.company_id == tenant_id
    )
    if parsed is not None:
        stmt = stmt.where(User.id.in_(list(parsed)))
    if active_only:
        stmt = stmt.where(
            User.deleted_at.is_(None),
            func.coalesce(User.active, true()) == true(),
        )
    try:
        with contained_read(db):
            rows = db.execute(stmt).all()
    except SQLAlchemyError:  # noqa: SILENTFAIL1 — no labels: ids show bare, roster drops staff with no entries; logged
        log.exception(log_event, extra={"tenant_id": tenant_id})
        return {}
    out: dict[str, str] = {}
    for r in rows:
        label = r.full_name or r.name or r.username or r.email
        if label:
            key = parsed.get(r.id, str(r.id)) if parsed is not None else str(r.id)
            out[key] = str(label)
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
    tz_name = shop_tz_name(settings)
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
    tz_name = shop_tz_name(settings)
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


# ── Vacation allowance and balance ───────────────────────────────────────────
#
# The allowance is an annual grant in HOURS per calendar year (migration 115):
# the person's `users.vacation_allowance_hours` when set, else the shop's
# `app_settings.vacation_allowance_hours`, else none. No accrual, no carryover,
# no payout — v1 compares hours booked against that one number.
#
# Used = every live, closed `vacation` entry whose SHOP day falls in the year,
# summed in minutes. Holidays are not drawn from the allowance. An approved
# request stages its entries up front, so days still ahead are already in
# `used`; `scheduled` is the part of `used` after `as_of`, shown so a reader can
# tell booked from taken. Remaining is not clamped at zero: an overdrawn
# balance is a fact the office needs to see, not one to hide.

VACATION_TYPE = "vacation"
ALLOWANCE_PERSON = "person"
ALLOWANCE_SHOP = "shop"


def _hours(minutes: int) -> float:
    return float((Decimal(int(minutes)) / 60).quantize(Decimal("0.01"), ROUND_HALF_UP))


@dataclass(frozen=True)
class VacationBalance:
    technician_id: str
    #: The year the person's vacation year starts in (``window[0].year``):
    #: the calendar year under ``calendar_year``.
    year: int
    as_of: date
    allowance_hours: Decimal | None
    #: ``person`` (the user's override), ``shop`` (the default) or None.
    allowance_source: str | None
    used_minutes: int = 0
    scheduled_minutes: int = 0
    #: First and last shop day of the vacation year counted (both inclusive).
    window: tuple[date, date] | None = None

    @property
    def used_hours(self) -> float:
        return _hours(self.used_minutes)

    @property
    def scheduled_hours(self) -> float:
        return _hours(self.scheduled_minutes)

    @property
    def taken_hours(self) -> float:
        return _hours(self.used_minutes - self.scheduled_minutes)

    @property
    def remaining_hours(self) -> float | None:
        if self.allowance_hours is None:
            return None
        remaining = Decimal(self.allowance_hours) - Decimal(int(self.used_minutes)) / 60
        return float(remaining.quantize(Decimal("0.01"), ROUND_HALF_UP))

    @property
    def remaining_at_as_of_hours(self) -> float | None:
        """Allowance minus what was taken through ``as_of``: the balance a
        past pay period's file can state and still state the same later."""
        if self.allowance_hours is None:
            return None
        taken = Decimal(int(self.used_minutes - self.scheduled_minutes)) / 60
        return float((Decimal(self.allowance_hours) - taken).quantize(Decimal("0.01"), ROUND_HALF_UP))

    @property
    def overdrawn(self) -> bool:
        remaining = self.remaining_hours
        return remaining is not None and remaining < 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "technician_id": self.technician_id,
            "year": self.year,
            "as_of": self.as_of.isoformat(),
            "allowance_hours": None if self.allowance_hours is None else float(self.allowance_hours),
            "allowance_source": self.allowance_source,
            "used_hours": self.used_hours,
            "taken_hours": self.taken_hours,
            "scheduled_hours": self.scheduled_hours,
            "remaining_hours": self.remaining_hours,
            "remaining_at_as_of_hours": self.remaining_at_as_of_hours,
            "overdrawn": self.overdrawn,
            "window_start": self.window[0].isoformat() if self.window else None,
            "window_end": self.window[1].isoformat() if self.window else None,
        }


# Which year a vacation allowance counts over (GDXA-1827, Doug 2026-10-19):
# each company chooses (AppSettings.vacation_year_policy). Calendar year is
# the default, and the anniversary policy falls back to the calendar year for
# anyone with no hire date. Every balance reader goes through
# `vacation_window` (GDXA-1855).
VACATION_YEAR_CALENDAR = "calendar_year"
VACATION_YEAR_ANNIVERSARY = "hire_anniversary"
VACATION_YEAR_POLICIES = (VACATION_YEAR_CALENDAR, VACATION_YEAR_ANNIVERSARY)


def vacation_year_policy_out(value: str | None) -> str:
    """The stored policy, or the calendar year for a missing or unknown value."""
    return value if value in VACATION_YEAR_POLICIES else VACATION_YEAR_CALENDAR


def _anniversary(hire_date: date, year: int) -> date:
    """The hire date's anniversary in ``year``; 29 February is 28 February
    in a year without one."""
    if hire_date.month == 2 and hire_date.day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return hire_date.replace(year=year)


def vacation_year_start(policy: str | None, hire_date: date | None, on: date) -> date:
    """First day of the vacation year that contains ``on`` for one person.

    ``hire_anniversary`` counts from the most recent anniversary of the hire
    date on or before ``on`` (a 29 February hire date falls on 28 February in
    other years). With no hire date, or one after ``on``, it is the calendar
    year, as is every other policy.
    """
    if not _counts_from_anniversary(policy, hire_date, on):
        return date(on.year, 1, 1)
    start = _anniversary(hire_date, on.year)
    return start if start <= on else _anniversary(hire_date, on.year - 1)


def _counts_from_anniversary(policy: str | None, hire_date: date | None, on: date) -> bool:
    return (
        vacation_year_policy_out(policy) == VACATION_YEAR_ANNIVERSARY
        and hire_date is not None
        and hire_date <= on
    )


def vacation_window(policy: str | None, hire_date: date | None, on: date) -> tuple[date, date]:
    """(first day, last day) of the vacation year that contains ``on``.

    The last day is the day before the next year starts: Dec 31 for a
    calendar year, the day before the next anniversary otherwise. A hire date
    after ``on`` means no anniversary has passed, so ``on`` is counted in the
    calendar year (the same fallback as no hire date)."""
    start = vacation_year_start(policy, hire_date, on)
    if not _counts_from_anniversary(policy, hire_date, on):
        return start, date(on.year, 12, 31)
    return start, _anniversary(hire_date, start.year + 1) - timedelta(days=1)


def vacation_windows(
    db: Session, settings: AppSettings | None, technician_ids: set[str], *, on: date
) -> dict[str, tuple[date, date]]:
    """technician_id → the vacation year containing ``on`` for that person,
    under the shop's policy and the person's ``hire_date``. Soft-deleted users
    are read too, as `allowance_overrides` does. An id with no user row gets
    the calendar year. Database errors propagate."""
    policy = vacation_year_policy_out(getattr(settings, "vacation_year_policy", None))
    hires: dict[str, date | None] = {}
    if policy == VACATION_YEAR_ANNIVERSARY:
        parsed = _parse_user_ids(technician_ids)
        if parsed:
            rows = db.execute(
                select(User.id, User.hire_date).where(
                    User.id.in_(list(parsed)), User.hire_date.is_not(None)
                )
            ).all()
            hires = {parsed.get(uid, str(uid)): hired for uid, hired in rows}
    return {tid: vacation_window(policy, hires.get(tid), on) for tid in technician_ids}


def _paid_vacation_between(lo: str, hi: str) -> tuple[Any, ...]:
    """Live, closed vacation entries whose ``clock_in_at`` (UTC text) is in
    [lo, hi): the coarse filter; callers decide the shop day themselves."""
    return (
        TimeclockEntry.deleted_at.is_(None),
        TimeclockEntry.entry_type == VACATION_TYPE,
        TimeclockEntry.clock_out_at.is_not(None),
        TimeclockEntry.minutes.is_not(None),
        TimeclockEntry.clock_in_at >= lo,
        TimeclockEntry.clock_in_at < hi,
    )


def vacation_minutes_by_person(
    db: Session,
    windows: dict[str, tuple[date, date]],
    tz_name: Any,
    *,
    as_of: date,
) -> dict[str, tuple[int, int]]:
    """technician_id → (used minutes, of which after ``as_of``), each person
    counted over their own vacation year (``windows``, from `vacation_windows`).

    The query is widened a day each side and the exact window decided in shop
    time, because ``clock_in_at`` is UTC text. Only people with a vacation
    entry inside their window are returned. Raises on a database error: a
    balance that silently reads zero used would tell someone they have hours
    they have already spent.
    """
    windows = {str(t): w for t, w in windows.items() if t}
    if not windows:
        return {}
    lo = (min(w[0] for w in windows.values()) - timedelta(days=1)).isoformat()
    hi = (max(w[1] for w in windows.values()) + timedelta(days=2)).isoformat()
    stmt = select(
        TimeclockEntry.technician_id, TimeclockEntry.clock_in_at, TimeclockEntry.minutes
    ).where(
        *_paid_vacation_between(lo, hi),
        TimeclockEntry.technician_id.in_(sorted(windows)),
    )
    out: dict[str, tuple[int, int]] = {}
    for tech, clock_in, minutes in db.execute(stmt).all():
        day = shop_day_of(clock_in, tz_name)
        window = windows.get(str(tech)) if tech else None
        if day is None or window is None or not window[0] <= day <= window[1]:
            continue
        paid = max(0, int(minutes))
        used, ahead = out.get(str(tech), (0, 0))
        out[str(tech)] = (used + paid, ahead + (paid if day > as_of else 0))
    return out


def vacation_takers(db: Session, *, on: date) -> set[str]:
    """Everyone with a closed, live vacation entry within a year either side
    of ``on``: the candidates whose vacation year containing ``on`` may hold
    one (any such year lies inside that span). Callers narrow it with
    `vacation_minutes_by_person`. Database errors propagate."""
    lo = (on - timedelta(days=367)).isoformat()
    hi = (on + timedelta(days=368)).isoformat()
    stmt = select(TimeclockEntry.technician_id).distinct().where(*_paid_vacation_between(lo, hi))
    return {str(t) for t in db.execute(stmt).scalars().all() if t}


def allowance_overrides(db: Session, technician_ids: set[str]) -> dict[str, Decimal]:
    """technician_id → the person's own allowance, for those who have one.

    Soft-deleted users are read too: a departed person's hours are still on
    the timesheet, and their balance should be stated the same way."""
    parsed = _parse_user_ids(technician_ids)
    if not parsed:
        return {}
    rows = db.execute(
        select(User.id, User.vacation_allowance_hours).where(
            User.id.in_(list(parsed)), User.vacation_allowance_hours.is_not(None)
        )
    ).all()
    return {parsed.get(uid, str(uid)): Decimal(value) for uid, value in rows}


def vacation_balances(
    db: Session,
    settings: AppSettings | None,
    technician_ids: set[str],
    *,
    on: date,
    as_of: date,
) -> dict[str, VacationBalance]:
    """One `VacationBalance` per id in ``technician_ids``, each over that
    person's vacation year containing ``on`` (`vacation_windows`: the
    calendar year, or the year from their hire anniversary under
    ``hire_anniversary``). Allowance, used and scheduled all follow it.

    The single computation behind the read endpoint, the Timesheets card,
    the pay-period CSV and the PDF context (via
    core/timesheet_hours.build_timesheet).
    Database errors propagate."""
    ids = {str(t) for t in technician_ids if t}
    if not ids:
        return {}
    tz_name = shop_tz_name(settings)
    shop_default = getattr(settings, "vacation_allowance_hours", None)
    overrides = allowance_overrides(db, ids)
    windows = vacation_windows(db, settings, ids, on=on)
    minutes = vacation_minutes_by_person(db, windows, tz_name, as_of=as_of)
    out: dict[str, VacationBalance] = {}
    for tid in ids:
        if tid in overrides:
            allowance, source = overrides[tid], ALLOWANCE_PERSON
        elif shop_default is not None:
            allowance, source = Decimal(shop_default), ALLOWANCE_SHOP
        else:
            allowance, source = None, None
        used, ahead = minutes.get(tid, (0, 0))
        out[tid] = VacationBalance(
            technician_id=tid,
            year=windows[tid][0].year,
            as_of=as_of,
            allowance_hours=allowance,
            allowance_source=source,
            used_minutes=used,
            scheduled_minutes=ahead,
            window=windows[tid],
        )
    return out
