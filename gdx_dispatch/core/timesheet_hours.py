"""Hours for a pay period — the numbers that leave the building.

The office Timesheets screen computes its own totals in JavaScript from the
raw `/entries` payload, and that stays true: it renders per-shift detail and
needs the rows anyway. This module is the authority for everything that
leaves the app — the CSV, the PDF, the email — and its rules are the SAME
rules, stated once here so a reviewer can check them against
`TimesheetsView.vue` line by line:

  worked minutes = max(0, minutes - break_minutes)
      `minutes` is what the clock recorded end to end. Totalling it pays out
      every lunch.

  flagged, matching `/exceptions` exactly and nothing more:
      open_shift        — no clock-out AND open longer than a possible shift.
                          Flagging every null clock_out would light up every
                          tech currently working; at 10am with four on the
                          clock the page would read "4 to fix" while Dispatch
                          reads 0, and the office would learn to ignore it.
      unknown_duration  — clocked out but `minutes` is NULL.
      implausible       — longer than MAX_SHIFT_HOURS.

  a shift belongs to the SHOP's calendar day, not the UTC one.

`gdx_dispatch/frontend/src/views/TimesheetsView.vue` carries a pointer back
here. If you change a rule, change it in both places or the screen and the
emailed file will disagree about what somebody worked — which is the
failure this whole feature exists to avoid.

Nothing here computes money. Hours are the deliverable; the bookkeeper
applies rates. That is also why there is no overtime split: this shop's
overtime is not a fact the clock knows, and inventing one would be the
"code may not invent hours" rule broken in a new place.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.pay_periods import PayPeriod, shop_day_of
from gdx_dispatch.models.tenant_models import TimeclockBreak, TimeclockEntry

log = logging.getLogger(__name__)

# Kept here rather than in routers/timeclock.py so the export and the router
# cannot drift to different thresholds; timeclock.py re-exports these names,
# which is why its own callers and tests are untouched.
WARNING_AFTER_HOURS = 8.0
MAX_SHIFT_HOURS = 16.0
IMPLAUSIBLE_SHIFT_MINUTES = int(MAX_SHIFT_HOURS * 60)

FLAG_OPEN = "open_shift"
FLAG_UNKNOWN = "unknown_duration"
FLAG_IMPLAUSIBLE = "implausible"

FLAG_LABELS = {
    FLAG_OPEN: "still clocked in",
    FLAG_UNKNOWN: "clocked out, no duration recorded",
    FLAG_IMPLAUSIBLE: "longer than a possible shift",
}

#: Same ceiling the office timesheet read uses. A whole shop punching four
#: times a day for a month is ~1,200 rows, so reaching this means something
#: is wrong rather than something is busy.
_ROW_CAP = 5000


@dataclass
class Shift:
    """One clock entry, already bucketed into a shop-local day."""

    entry_id: str
    day: date
    clock_in: str
    clock_out: str | None
    minutes: int | None
    break_minutes: int
    entry_type: str
    notes: str | None
    flag: str | None

    @property
    def worked_minutes(self) -> int:
        """Minutes to pay for. An unknown duration counts as zero, and is
        flagged — a guessed number would be indistinguishable from a real
        one once it is in a spreadsheet."""
        if self.minutes is None:
            return 0
        return max(0, int(self.minutes) - int(self.break_minutes or 0))

    @property
    def worked_hours(self) -> float:
        return round(self.worked_minutes / 60.0, 2)


@dataclass
class Timecard:
    """One person's shifts inside one pay period."""

    tech_id: str
    name: str
    shifts: list[Shift] = field(default_factory=list)

    @property
    def worked_minutes(self) -> int:
        return sum(s.worked_minutes for s in self.shifts)

    @property
    def worked_hours(self) -> float:
        return round(self.worked_minutes / 60.0, 2)

    @property
    def break_minutes(self) -> int:
        return sum(int(s.break_minutes or 0) for s in self.shifts)

    @property
    def flagged(self) -> list[Shift]:
        return [s for s in self.shifts if s.flag]

    def by_day(self) -> dict[date, list[Shift]]:
        out: dict[date, list[Shift]] = {}
        for s in self.shifts:
            out.setdefault(s.day, []).append(s)
        return out


@dataclass
class PeriodTimesheet:
    """Everything the export, the PDF and the send gate need."""

    period: PayPeriod
    timezone: str
    timecards: list[Timecard] = field(default_factory=list)

    @property
    def worked_hours(self) -> float:
        return round(sum(t.worked_minutes for t in self.timecards) / 60.0, 2)

    @property
    def people(self) -> int:
        return len(self.timecards)

    @property
    def shift_count(self) -> int:
        return sum(len(t.shifts) for t in self.timecards)

    @property
    def flagged(self) -> list[tuple[Timecard, Shift]]:
        return [(t, s) for t in self.timecards for s in t.flagged]

    @property
    def is_clean(self) -> bool:
        """True when nothing in this period needs a human to look at it.

        The scheduled send asks exactly this before mailing anything.
        """
        return not self.flagged


def flag_for(
    *,
    clock_out: str | None,
    minutes: int | None,
    clock_in: str | None,
    now: datetime | None = None,
) -> str | None:
    """The single reason this shift needs a look, or None.

    Mirrors `isFlagged()` in TimesheetsView.vue and the three kinds
    `/exceptions` reports. Ordered: an open shift is never also "unknown
    duration", because it has not ended yet.
    """
    if not clock_out:
        started = _as_aware(clock_in)
        if started is None:
            return FLAG_OPEN
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        open_hours = (moment - started).total_seconds() / 3600.0
        return FLAG_OPEN if open_hours > MAX_SHIFT_HOURS else None
    if minutes is None:
        return FLAG_UNKNOWN
    if int(minutes) > IMPLAUSIBLE_SHIFT_MINUTES:
        return FLAG_IMPLAUSIBLE
    return None


def _as_aware(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).strip().replace(" ", "T"))
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def build_timesheet(
    db: Session,
    *,
    tenant_id: str,
    period: PayPeriod,
    tz_name: str,
    names: dict[str, str] | None = None,
    tech_id: str | None = None,
    now: datetime | None = None,
) -> PeriodTimesheet:
    """Every shift worked in `period`, grouped by person, in shop time.

    The query widens by a day on each end and the exact boundary is applied
    afterwards in shop time. `clock_in_at` is TEXT holding an ISO instant in
    UTC, so filtering it directly buckets by the UTC day and drops an
    evening shift out of the period it was actually worked.
    """
    names = names or {}
    # A day of slack on each end, then the exact boundary re-applied in shop
    # time below. Lexical comparison is correct on ISO-8601 text, so the
    # database still does the coarse filter.
    lo_text = date.fromordinal(period.start.toordinal() - 1).isoformat()
    hi_text = date.fromordinal(period.end.toordinal() + 2).isoformat()

    stmt = (
        select(TimeclockEntry)
        .where(
            TimeclockEntry.tenant_id == tenant_id,
            TimeclockEntry.deleted_at.is_(None),
            TimeclockEntry.clock_in_at >= lo_text,
            TimeclockEntry.clock_in_at < hi_text,
        )
        .order_by(TimeclockEntry.clock_in_at.asc())
        .limit(_ROW_CAP)
    )
    if tech_id:
        stmt = stmt.where(TimeclockEntry.technician_id == tech_id)

    try:
        rows = db.execute(stmt).scalars().all()
    except SQLAlchemyError:
        # Callers must not paper over this with an empty timesheet — a
        # payroll file reporting zero hours it never queried is worse than
        # no file at all.
        raise

    # Gross `minutes` never has break time subtracted — that lives in its own
    # table — so any surface reporting worked hours must net it or it pays out
    # every lunch.
    # Clamp the break window to the period this timesheet covers, so an open
    # shift cannot drag in breaks taken after the period ended.
    breaks = break_minutes_by_entry(db, tenant_id, list(rows), window_hi=hi_text)

    cards: dict[str, Timecard] = {}
    for row in rows:
        day = shop_day_of(row.clock_in_at, tz_name)
        if day is None or not period.contains(day):
            continue
        tid = str(row.technician_id or "")
        if not tid:
            continue
        card = cards.get(tid)
        if card is None:
            card = Timecard(tech_id=tid, name=names.get(tid) or _short_id(tid))
            cards[tid] = card
        card.shifts.append(
            Shift(
                entry_id=str(row.id),
                day=day,
                clock_in=str(row.clock_in_at),
                clock_out=str(row.clock_out_at) if row.clock_out_at else None,
                minutes=int(row.minutes) if row.minutes is not None else None,
                break_minutes=int(breaks.get(str(row.id), 0) or 0),
                entry_type=str(row.entry_type or "clock"),
                notes=row.notes,
                flag=flag_for(
                    clock_out=row.clock_out_at,
                    minutes=row.minutes,
                    clock_in=row.clock_in_at,
                    now=now,
                ),
            )
        )

    ordered = sorted(cards.values(), key=lambda c: (c.name.lower(), c.tech_id))
    return PeriodTimesheet(period=period, timezone=str(tz_name), timecards=ordered)


def _short_id(tech_id: str) -> str:
    """What to call someone whose user row is gone.

    Their hours are still on the books, so the row must appear — named as
    honestly as we can rather than dropped.
    """
    return f"Unknown ({tech_id[:8]}…)"


def break_minutes_started_on(
    db: Session,
    tenant_id: str,
    user_id: str,
    entries: list[TimeclockEntry],
    day_iso: str,
) -> int:
    """Ended-break minutes for `user_id` that STARTED on `day_iso`.

    `break_minutes_by_entry` attributes a break to the shift it happened during,
    which is the right rule for a timesheet. A "hours today" figure needs the
    other cut: an open overnight shift contributes only its post-midnight
    portion, so charging it the whole shift's breaks would take a 19:00 lunch
    off today *and* off yesterday's own timesheet — the same 30 minutes twice,
    in opposite days.
    """
    if not entries:
        return 0
    by_entry = break_minutes_by_entry(db, tenant_id, entries)
    if not by_entry:
        return 0
    try:
        rows = db.execute(
            select(TimeclockBreak.id, TimeclockBreak.duration_minutes).where(
                TimeclockBreak.tenant_id == tenant_id,
                TimeclockBreak.user_id == str(user_id),
                TimeclockBreak.duration_minutes.isnot(None),
                func.date(TimeclockBreak.started_at) == day_iso,
            )
        ).all()
    except SQLAlchemyError:
        log.exception("break_minutes_started_on_failed", extra={"tenant_id": tenant_id})
        return 0
    # Cap at what the shift-attribution already counted, so a break belonging to
    # nobody's shift cannot subtract from the day.
    attributable = sum(int(v or 0) for v in by_entry.values())
    return min(sum(int(m or 0) for _id, m in rows), attributable)


def open_break_in_shift(
    db: Session, tenant_id: str, entry: TimeclockEntry
) -> TimeclockBreak | None:
    """The break running INSIDE this shift, or None.

    Bounded to the shift on purpose. Nothing closes a break AUTOMATICALLY:
    `POST /api/timeclock/break/end` exists and both timeclock screens call it,
    but `post_clock_out` does not touch breaks, the clock-in auto-close closes
    only the shift, and there is no sweep — so a break the tech never ends
    stays open forever. Prod carries an open `lunch` row started 2026-04-08
    (measured 2026-09-07). An unbounded "is there an open break for this user"
    lookup therefore reports *permanently on break*, which on a worked-hours
    surface reads as zero paid time — #645 pointed the other way, and
    under-reporting someone's pay is the worse direction.

    This bound covers the ABANDONED break. A break forgotten *inside* the
    current shift still freezes worked time until it is ended — which is the
    honest reading of the record, and why the card that shows it must offer a
    way to end it rather than leaving the tech at a dead end.

    A break that started before this shift did is not this shift's break.
    """
    started = _as_aware(entry.clock_in_at)
    if started is None:
        return None
    try:
        rows = db.execute(
            select(TimeclockBreak)
            .where(
                TimeclockBreak.tenant_id == tenant_id,
                TimeclockBreak.user_id == str(entry.technician_id or ""),
                TimeclockBreak.ended_at.is_(None),
            )
            .order_by(TimeclockBreak.started_at.desc())
            .limit(10)
        ).scalars().all()
    except SQLAlchemyError:
        log.exception("open_break_lookup_failed", extra={"tenant_id": tenant_id})
        return None
    for row in rows:
        when = _as_aware(row.started_at)
        # `>=` not `>`: a break stamped in the same instant as the clock-in is
        # this shift's. Compared in aware UTC, never as text — the stamps are
        # TEXT and a naive string compare would order "+00:00" against a naive
        # one wrongly.
        if when is not None and when >= started:
            return row
    return None


def open_shift_worked_minutes(
    db: Session,
    tenant_id: str,
    entry: TimeclockEntry,
    *,
    now: datetime | None = None,
) -> tuple[int, int, int, TimeclockBreak | None]:
    """``(worked, gross, ended_break_minutes, open_break)`` for an OPEN shift.

    ONE implementation of the rule, called by both the mobile job card and
    `/api/timeclock/status`. An earlier revision computed the same thing two
    different ways in two files, which is the exact drift this module's
    docstring warns about.

    Worked time stops at an open break's START — a known stamp, so nothing is
    guessed — and ended breaks come off as usual. Gross is the wall clock and
    keeps running, so a caller can show both without them contradicting.
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    started = _as_aware(entry.clock_in_at)
    if started is None:
        # FOUR values on every path. An earlier revision returned three here
        # and four below; both callers unpack four, and /status's handler
        # catches only SQLAlchemyError, so the ValueError became a 500 on the
        # endpoint every client polls on load.
        return 0, 0, 0, None
    gross = int(max((moment - started).total_seconds(), 0) // 60)
    ended = int(break_minutes_by_entry(db, tenant_id, [entry]).get(str(entry.id), 0) or 0)
    on_break = open_break_in_shift(db, tenant_id, entry)
    stop = moment
    if on_break is not None:
        when = _as_aware(on_break.started_at)
        if when is not None:
            stop = when
    worked = int(max((stop - started).total_seconds(), 0) // 60) - ended
    # `ended` is returned separately rather than left for the caller to derive
    # as gross-minus-worked: during an open break that difference also contains
    # the running break, so a caller printing it as "less Xm on break" would
    # state a number no break record supports.
    return max(worked, 0), gross, ended, on_break


def break_minutes_by_entry(
    db: Session,
    tenant_id: str,
    entries: list[TimeclockEntry],
    *,
    window_hi: str | None = None,
) -> dict[str, int]:
    """{entry_id: total ended-break minutes} for the given entries.

    `TimeclockEntry.minutes` is gross elapsed — clock-out writes
    `_minutes_between(clock_in, now)` and never subtracts breaks, which live in
    their own table. Any surface reporting worked hours has to do this or it
    pays out every lunch.

    Matched by user + time window, NOT by `timeclock_breaks_router.time_entry_id`.
    That column exists and `POST /break/start` will store it, but it is optional
    and BOTH clients (TimeclockView, MobileTimeclockView) post `{}` — so it is
    NULL on 10 of 10 real rows and a join on it returns nothing. It is still
    honored first, so the link sharpens for free if a client ever starts
    sending it.

    Open breaks (ended_at NULL → duration_minutes NULL) contribute 0. An
    unfinished break has no defensible length, and inventing one would fabricate
    hours — the same rule that makes an auto-closed shift report "unknown"
    rather than its elapsed time.
    """
    if not entries:
        return {}
    tech_ids = {str(e.technician_id) for e in entries if e.technician_id}
    if not tech_ids:
        return {}
    # Bound to the window the entries actually span. Without this the query
    # pulls every break the whole crew has ever taken on every page load, to
    # then discard all but the overlapping ones.
    span_lo = min((str(e.clock_in_at) for e in entries if e.clock_in_at), default=None)
    # An OPEN entry runs until `window_hi` (the caller's ceiling) or now — not
    # to its clock-in. Using clock_in_at as the upper bound truncated the SQL
    # filter to the day the shift STARTED, so every break an open overnight
    # shift took after midnight was discarded here, before the per-row overlap
    # logic below (which already ends an open window at `now`) could match it.
    # That path is live: `build_timesheet` selects by clock_in_at range with no
    # clock_out filter, so a payroll timesheet containing an open shift silently
    # paid out its next-day lunches.
    #
    # `window_hi` is why this is not simply `now`: over a PAST pay period
    # containing an open shift, an unclamped ceiling sweeps in every break the
    # tech has taken since, and the per-row matcher attributes them all to that
    # open entry — putting break minutes from after the period into the
    # period's CSV and PDF (`core/timesheet_export.py` renders `break_minutes`
    # per shift and per timecard). `build_timesheet` passes its own `hi_text`.
    # Found by the #645 sibling sweep, 2026-09-07.
    span_hi = max(
        (
            str(e.clock_out_at) if e.clock_out_at else (window_hi or datetime.now(UTC).isoformat())
            for e in entries
            if e.clock_in_at
        ),
        default=None,
    )
    try:
        clauses = [
            TimeclockBreak.tenant_id == tenant_id,
            TimeclockBreak.user_id.in_(tech_ids),
            TimeclockBreak.duration_minutes.isnot(None),
        ]
        if span_lo and span_hi:
            # Date-level bound only — the exact overlap is decided per row below
            # against each shift's real window, so this just stops the query
            # scanning years of breaks to discard them.
            clauses.append(func.date(TimeclockBreak.started_at) >= span_lo[:10])
            clauses.append(func.date(TimeclockBreak.started_at) <= span_hi[:10])
        rows = db.execute(
            select(
                TimeclockBreak.time_entry_id,
                TimeclockBreak.user_id,
                TimeclockBreak.started_at,
                TimeclockBreak.duration_minutes,
            ).where(*clauses)
        ).all()
    except SQLAlchemyError:
        # Never fail the timesheet over this — gross hours are still correct and
        # still correctable. Logged so a silent overstatement is traceable.
        log.exception("timeclock_break_join_failed", extra={"tenant_id": tenant_id})
        return {}
    if not rows:
        return {}

    # Per-tech shift windows, so a break lands on the shift it happened during.
    windows: dict[str, list[tuple[datetime, datetime, str]]] = {}
    by_id: set[str] = set()
    for e in entries:
        by_id.add(str(e.id))
        start = _as_aware(e.clock_in_at)
        end = _as_aware(e.clock_out_at) if e.clock_out_at else datetime.now(UTC)
        if start is None or end is None:
            # An unparseable stamp has no window, so no break can be matched
            # to it. Skipping is right: netting a break onto a shift we cannot
            # place would subtract paid time from the wrong day.
            continue
        windows.setdefault(str(e.technician_id), []).append((start, end, str(e.id)))

    totals: dict[str, int] = {}
    for entry_id, user_id, started_at, minutes in rows:
        target = str(entry_id) if entry_id and str(entry_id) in by_id else None
        if target is None:
            started = _as_aware(started_at)
            if started is None:
                continue
            for start, end, eid in windows.get(str(user_id), ()):
                if start <= started <= end:
                    target = eid
                    break
        if target is None:
            # A break outside every returned shift — usually just a shift that
            # fell outside the requested range. Dropping it is right: it must
            # not be netted off some unrelated day.
            continue
        totals[target] = totals.get(target, 0) + int(minutes or 0)
    return totals
