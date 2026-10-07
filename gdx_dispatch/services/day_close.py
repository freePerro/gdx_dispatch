"""Day-close reads for multi-day jobs (the multi-day jobs plan, §5.4a).

"No" on the closeout sheet closes one shop day of a job: its visits, and the
hours of each person who tapped in that day. Everything here is a READ that
the day-close route, the day log, the closeout ("Yes"), ``/complete`` and
``/close-without-work`` share, so the sheet and the server ask one predicate.

Vocabulary (the spec's, used verbatim):

* A **candidate timer on day D** is a job timer (``entry_type='job'``) on this
  job, not deleted, not yet day-closed, whose ``clock_in`` is on shop day D,
  that is either open or Stop-marked with no minutes, and that started after
  the job's latest finish (``latest_finish``).
* A **day row** is a ``time_entries`` row with ``day_closed_at`` set and
  minutes > 0: one person's attested hours for one shop day.
* **Current** visits are ``visit_state`` open or on-site, and not deleted.

Shop days are computed in Python from the stored instants (``shop_day``), never
by a SQL date function, so SQLite and Postgres agree; a job holds few rows.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Appointment, JobCloseout, Technician, TimeEntry
from gdx_dispatch.services.visit_sync import (
    ON_SITE,
    is_current,
    shop_day,
    shop_tz,
    visit_rows,
    visit_state,
)

__all__ = [
    "ADDED_HELPER_NOTE",
    "DAY_CLOSED_ACTION",
    "FINISH_ACTIONS",
    "aware",
    "candidate_timers",
    "current_visits",
    "day_close_audits",
    "day_log",
    "day_rows",
    "earlier_day_open",
    "is_candidate",
    "latest_finish",
    "later_day_started",
    "open_day",
    "row_owners",
    "shop_tz",
    "tech_names",
    "today_closed_by_no",
    "user_names",
]

DAY_CLOSED_ACTION = "job_day_closed"
#: The two finishing routes that write no closeout row. Their audit rows are the
#: durable record of the finish: a re-open clears ``jobs.completed_at``.
FINISH_ACTIONS = ("job_completed", "job_closed_without_work")
ADDED_HELPER_NOTE = "Added helper (not tapped in)"


def aware(value: datetime | None) -> datetime | None:
    """SQLite hands timestamps back naive; every writer stores UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _stop_note() -> str:
    # mobile.py owns the marker its own Stop writer stamps.
    from gdx_dispatch.routers.mobile import MOBILE_STOP_LABOR_NOTE  # noqa: PLC0415

    return MOBILE_STOP_LABOR_NOTE


def _job_audits(db: Session, job: Any, actions: tuple[str, ...]) -> list[Any]:
    """The job's audit rows for ``actions``, oldest first. ``entity_id`` is a
    String column holding the dashed id every job writer stores, so it is
    compared to ``str(job.id)``, never to a Uuid value."""
    from gdx_dispatch.core.audit import AuditLog  # noqa: PLC0415

    return list(db.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "job",
            AuditLog.entity_id == str(job.id),
            AuditLog.action.in_(actions),
        ).order_by(AuditLog.created_at, AuditLog.id)
    ).scalars().all())


def latest_finish(db: Session, job: Any) -> datetime | None:
    """The newest of the job's closeout rows' ``created_at`` and its newest
    ``job_completed`` / ``job_closed_without_work`` audit row's ``created_at``.
    A timer started at or before it belongs to a finish that already billed
    (round 34/35), so it is never a candidate again after a re-open."""
    moments: list[datetime] = []
    newest_closeout = db.execute(
        select(JobCloseout.created_at)
        .where(JobCloseout.job_id == job.id)
        .order_by(JobCloseout.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if newest_closeout is not None:
        moments.append(aware(newest_closeout))
    for row in _job_audits(db, job, FINISH_ACTIONS):
        if row.created_at is not None:
            moments.append(aware(row.created_at))
    return max(moments) if moments else None


def is_candidate(t: TimeEntry, stop_note: str) -> bool:
    """Open, or Stop-marked with 0 or no minutes; never a day-closed row."""
    if t.deleted_at is not None or t.day_closed_at is not None or t.entry_type != "job":
        return False
    if t.clock_out is None:
        return True
    return (t.notes or "").startswith(stop_note) and not (t.duration_minutes or 0)


def _all_candidates(db: Session, job: Any) -> list[TimeEntry]:
    """Every candidate timer on the job, whatever its day, oldest first."""
    stop = _stop_note()
    rows = db.execute(
        select(TimeEntry).where(
            TimeEntry.job_id == job.id,
            TimeEntry.entry_type == "job",
            TimeEntry.deleted_at.is_(None),
            TimeEntry.day_closed_at.is_(None),
        ).order_by(TimeEntry.clock_in, TimeEntry.id)
    ).scalars().all()
    bound = latest_finish(db, job)
    return [
        t for t in rows
        if is_candidate(t, stop) and (bound is None or aware(t.clock_in) > bound)
    ]


def candidate_timers(db: Session, job: Any, day: date, tz: str) -> list[TimeEntry]:
    """The candidate timers on shop day ``day``, oldest ``clock_in`` first.

    Its own query, not ``_stopped_job_timer_for``: there is no 24 h window,
    because a forgotten day can close days later. A timer whose ``clock_in``
    is on another shop day is never a candidate, so a day-2 "No" replayed on
    day 3 cannot touch day 3's timer."""
    return [t for t in _all_candidates(db, job) if shop_day(aware(t.clock_in), tz) == day]


def day_rows(db: Session, job_id: Any) -> list[TimeEntry]:
    """The job's day rows: ``day_closed_at`` set, minutes > 0, not deleted,
    closed. A timer consumed at 0 carries the marker but is not one."""
    jid = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))
    return list(db.execute(
        select(TimeEntry).where(
            TimeEntry.job_id == jid,
            TimeEntry.day_closed_at.is_not(None),
            TimeEntry.duration_minutes > 0,
            TimeEntry.deleted_at.is_(None),
            TimeEntry.clock_out.is_not(None),
        ).order_by(TimeEntry.clock_in.desc(), TimeEntry.id)
    ).scalars().all())


def current_visits(db: Session, job: Any) -> list[Appointment]:
    """Current visits: open or on site, and not deleted (``visit_rows``
    filters ``deleted_at``; ``is_current`` never reads it)."""
    return [v for v in visit_rows(db, job.id) if v.deleted_at is None and is_current(v)]


def _worked_past_days(db: Session, job: Any, today: date, tz: str) -> list[date]:
    """Shop days before ``today`` with a Current ON_SITE visit or a candidate
    timer. A past day with neither is a no-show, not this sheet's to close."""
    days: set[date] = set()
    for v in current_visits(db, job):
        if visit_state(v) == ON_SITE:
            d = shop_day(aware(v.start_at), tz)
            if d is not None and d < today:
                days.add(d)
    for t in _all_candidates(db, job):
        d = shop_day(aware(t.clock_in), tz)
        if d is not None and d < today:
            days.add(d)
    return sorted(days)


def earlier_day_open(db: Session, job: Any, today: date, tz: str) -> date | None:
    """The open day's date when it blocks "Yes", else None.

    ``open_day.date`` is before ``today`` AND either it is before the job's
    latest Current visit's shop day, or a day-close closed a visit on it
    (``appointments.day_closed_at``). Keyed on a day-close, not on "no Current
    visit", so a job with no visit (an A3 timer) or one whose only visit the
    office Completed by hand still closes out the next morning (round 34)."""
    worked = _worked_past_days(db, job, today, tz)
    if not worked:
        return None
    day = worked[0]
    current_days = [shop_day(aware(v.start_at), tz) for v in current_visits(db, job)]
    if current_days and day < max(d for d in current_days if d is not None):
        return day
    for v in visit_rows(db, job.id):
        if v.day_closed_at is not None and shop_day(aware(v.start_at), tz) == day:
            return day
    return None


def later_day_started(db: Session, job: Any, tap_day: date, tz: str) -> date | None:
    """The earliest shop day after ``tap_day`` with a Current ON_SITE visit or
    a candidate timer (Start needs only en route, and a manual clock-in never
    stamps a visit — round 34), else None."""
    days: set[date] = set()
    for v in current_visits(db, job):
        if visit_state(v) == ON_SITE:
            d = shop_day(aware(v.start_at), tz)
            if d is not None and d > tap_day:
                days.add(d)
    for t in _all_candidates(db, job):
        d = shop_day(aware(t.clock_in), tz)
        if d is not None and d > tap_day:
            days.add(d)
    return min(days) if days else None


def day_close_audits(db: Session, job: Any) -> list[Any]:
    """The job's ``job_day_closed`` audit rows, oldest first."""
    return _job_audits(db, job, (DAY_CLOSED_ACTION,))


def today_closed_by_no(db: Session, job: Any, tap_day: date, tz: str) -> bool:
    """The 0 h "Yes" rule (Doug, 2026-10-06): a ``job_day_closed`` audit row
    whose ``details.day`` is the tap's shop day — read in Python, not through
    a JSON operator, so SQLite and Postgres agree — AND no candidate timer on
    that day (a helper who tapped in again, or a person the "No" left
    unlisted, would otherwise be closed at 0)."""
    key = tap_day.isoformat()
    closed = any(
        isinstance(row.details, dict) and row.details.get("day") == key
        for row in day_close_audits(db, job)
    )
    return closed and not candidate_timers(db, job, tap_day, tz)


def user_names(db: Session, user_ids: set[str]) -> dict[str, str | None]:
    """users.id → display name, falling back to the technician's name."""
    from gdx_dispatch.models.tenant_models import User  # noqa: PLC0415

    names: dict[str, str | None] = {u: None for u in user_ids if u}
    parseable: dict[str, uuid.UUID] = {}
    for uid in names:
        try:
            parseable[uid] = uuid.UUID(uid)
        except (ValueError, AttributeError):
            continue
    if parseable:
        by_uuid = {
            u.id: (u.full_name or u.name or u.username or u.email)
            for u in db.execute(
                select(User).where(User.id.in_(list(parseable.values())))
            ).scalars().all()
        }
        for uid, uu in parseable.items():
            names[uid] = by_uuid.get(uu)
    missing = [u for u, n in names.items() if not n]
    if missing:
        for t in db.execute(
            select(Technician).where(Technician.user_id.in_(missing), Technician.deleted_at.is_(None))
        ).scalars().all():
            if t.name and not names.get(str(t.user_id)):
                names[str(t.user_id)] = t.name
    return names


def tech_names(db: Session, tech_ids: set[str]) -> dict[str, str | None]:
    """technicians.id (a visit's ``tech_id``) → name."""
    ids = [t for t in tech_ids if t]
    if not ids:
        return {}
    return {
        str(t.id): t.name
        for t in db.execute(select(Technician).where(Technician.id.in_(ids))).scalars().all()
    }


def _logged_by_person(db: Session, job: Any, day: date) -> dict[str, list[dict]]:
    """``logged`` per person on ``day``, read from the ``job_day_closed``
    audit rows' ``people`` (user id, hours, day-row id, actor) — never matched
    by ``tech_id`` (round 34): an ``added`` row carries the submitter's id."""
    key = day.isoformat()
    live = {str(r.id) for r in day_rows(db, job.id)}
    hits: list[tuple[str, float, str | None]] = []
    for row in day_close_audits(db, job):
        details = row.details if isinstance(row.details, dict) else {}
        if details.get("day") != key:
            continue
        for p in details.get("people") or []:
            if not isinstance(p, dict) or not p.get("user_id"):
                continue
            if p.get("day_row_id") and str(p["day_row_id"]) not in live:
                continue
            hits.append((str(p["user_id"]), float(p.get("hours") or 0), row.user_id))
    closer_names = user_names(db, {c for _, _, c in hits if c})
    out: dict[str, list[dict]] = {}
    for uid, hours, closer in hits:
        out.setdefault(uid, []).append({"hours": hours, "closed_by": closer_names.get(closer or "")})
    return out


def open_day(db: Session, job: Any, user_id: str | None, today: date, tz: str) -> dict:
    """What the "No" sheet shows: the oldest past worked day (an ON_SITE
    Current visit or a candidate timer), else ``today``; its Current visits;
    and the people with a candidate timer on it, the caller first."""
    worked = _worked_past_days(db, job, today, tz)
    day = worked[0] if worked else today
    visits = [v for v in current_visits(db, job) if shop_day(aware(v.start_at), tz) == day]
    names = tech_names(db, {v.tech_id for v in visits if v.tech_id})
    people_ids = list(dict.fromkeys(
        str(t.user_id) for t in candidate_timers(db, job, day, tz) if t.user_id
    ))
    pnames = user_names(db, set(people_ids))
    logged = _logged_by_person(db, job, day)
    people = [
        {
            "user_id": uid,
            "name": pnames.get(uid),
            "mine": bool(user_id) and uid == str(user_id),
            "logged": logged.get(uid, []),
        }
        for uid in people_ids
    ]
    people.sort(key=lambda p: not p["mine"])
    return {
        "date": day.isoformat(),
        "visits": [
            {
                "id": str(v.id),
                "tech_name": names.get(str(v.tech_id)) if v.tech_id else None,
                "start_at": aware(v.start_at).isoformat() if v.start_at else None,
                "state": visit_state(v),
            }
            for v in visits
        ],
        "people": people,
    }


def row_owners(db: Session, job: Any) -> dict[str, tuple[str | None, str | None]]:
    """day-row id → (person user id, or ``"added"``; closer's user id), from
    the ``job_day_closed`` audit rows. The audit is the record of whose hours
    a row carries: a ``user_id`` NULL row's ``tech_id`` is a timer's
    ``tech_id`` or, for an added helper, the submitter's id (round 34)."""
    owners: dict[str, tuple[str | None, str | None]] = {}
    for row in day_close_audits(db, job):
        details = row.details if isinstance(row.details, dict) else {}
        for p in details.get("people") or []:
            if isinstance(p, dict) and p.get("day_row_id"):
                owners[str(p["day_row_id"])] = (str(p.get("user_id") or "") or None, row.user_id)
        for a in details.get("added") or []:
            if isinstance(a, dict) and a.get("day_row_id"):
                owners[str(a["day_row_id"])] = ("added", row.user_id)
    return owners


def day_log(db: Session, job: Any, tz: str) -> list[dict]:
    """The daily log: every day row, newest first, as
    ``{id, date, person_name, hours, note, closed_by}``. ``person_name`` is
    "Added helper" for an ``added`` row."""
    rows = day_rows(db, job.id)
    owners = row_owners(db, job)
    ids: set[str] = set()
    for r in rows:
        person, closer = owners.get(str(r.id), (None, None))
        if person and person != "added":
            ids.add(person)
        elif person is None and r.user_id:
            ids.add(str(r.user_id))
        if closer:
            ids.add(closer)
    names = user_names(db, ids)
    out = []
    for r in rows:
        person, closer = owners.get(str(r.id), (None, None))
        person_name = (
            "Added helper" if person == "added"
            else names.get(person or str(r.user_id or ""))
        )
        out.append({
            "id": str(r.id),
            "date": shop_day(aware(r.clock_in), tz).isoformat() if r.clock_in else None,
            "person_name": person_name,
            "hours": round((r.duration_minutes or 0) / 60, 2),
            "note": r.notes,
            "closed_by": names.get(closer) if closer else None,
        })
    return out
