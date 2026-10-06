"""A job's visits: the pure planner, its applier, and the job-date recompute.

Multi-day jobs plan §5.2a (revision 2). A *visit* is an ``appointments`` row
with ``job_id`` set and ``deleted_at`` null. ``plan_visit_sync`` reads visits
as plain values and returns either a list of actions or a refusal; it touches
no database, so the whole state table is tested without one
(``tests/test_visit_sync_grid.py``). ``apply_visit_plan`` writes the actions
and their audit rows. ``recompute_job_schedule`` keeps invariant I:

    On a job that is not completed or cancelled and holds a Current visit,
    ``scheduled_at`` equals the earliest Current visit's ``start_at``.

Every writer of a job's visits or date runs it last.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any
from uuid import UUID

OPEN = "open"
ON_SITE = "on_site"
CLOSED = "closed"
CANCELLED = "cancelled"

FINISHED_STAGES = ("completed", "cancelled")


class _Unset:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Any = _Unset()


@dataclass(frozen=True)
class VisitRow:
    id: Any
    tech_id: str | None
    start_at: datetime
    end_at: datetime | None = None
    status: str | None = "scheduled"
    arrived_at: datetime | None = None
    title: str | None = None
    customer_id: Any = None
    customer_name: str | None = None


def visit_state(v: Any) -> str:
    status = (v.status or "").lower()
    if status == "completed":
        return CLOSED
    if status == "cancelled":
        return CLOSED if v.arrived_at is not None else CANCELLED
    if v.arrived_at is not None or status == "arrived":
        return ON_SITE
    return OPEN


def is_current(v: Any) -> bool:
    return visit_state(v) in (OPEN, ON_SITE)


def is_live(v: Any) -> bool:
    return visit_state(v) != CANCELLED


def to_minute(value: datetime | None) -> datetime | None:
    return value.replace(second=0, microsecond=0) if value is not None else None


def _as_utc(value: datetime) -> datetime:
    # SQLite hands back naive datetimes; every writer stores UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


@lru_cache(maxsize=4096)
def shop_day(value: datetime | None, tz_name: str) -> date | None:
    from gdx_dispatch.core.pay_periods import shop_day_of  # noqa: PLC0415

    return shop_day_of(value, tz_name)


def current_day(visits: list[Any], tz_name: str) -> date | None:
    """N: the shop day of the earliest Current visit, or None."""
    current = [v for v in visits if is_current(v)]
    if not current:
        return None
    return shop_day(min(_as_utc(v.start_at) for v in current), tz_name)


# ---------------------------------------------------------------- actions


@dataclass(frozen=True)
class Insert:
    key: Any
    tech_id: str | None
    start_at: datetime
    end_at: datetime
    reason: str


@dataclass(frozen=True)
class Move:
    visit_id: Any
    start_at: datetime
    end_at: datetime
    reason: str


@dataclass(frozen=True)
class Reassign:
    visit_id: Any
    tech_id: str | None
    reason: str


@dataclass(frozen=True)
class Retire:
    visit_id: Any
    reason: str


@dataclass(frozen=True)
class Close:
    visit_id: Any
    status: str
    reason: str


@dataclass(frozen=True)
class CopyFields:
    visit_id: Any
    changes: dict


@dataclass(frozen=True)
class Refusal:
    code: str
    message: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class VisitEdit:
    """One request against a job's visits.

    ``scheduled_at`` is the requested date (UNSET: not in the request; None:
    cleared). ``crew_after`` None means no crew change. ``finish_day`` set
    means the job is being completed (F1). ``reopen`` means /uncomplete or
    /reactivate: R0 first, using ``job_state`` to pick F1 or X1.
    """

    tz_name: str
    job_state: str = "live"
    stored_scheduled_at: datetime | None = None
    scheduled_at: Any = UNSET
    crew_before: tuple = ()
    crew_after: tuple | None = None
    fields: dict | None = None
    cancel: bool = False
    finish_day: date | None = None
    reopen: bool = False
    reopen_day: date | None = None
    rebook_closed_day: bool | None = None
    duration_minutes: int = 60


@dataclass
class VisitPlan:
    actions: list = field(default_factory=list)
    refusal: Refusal | None = None
    # The job's new scheduled_at (minute-truncated), or UNSET when the
    # request does not write it.
    scheduled_at: Any = UNSET
    # The rows as the plan leaves them (retired rows dropped).
    visits: list = field(default_factory=list)


# ---------------------------------------------------------------- planner


class _Refused(Exception):
    def __init__(self, refusal: Refusal) -> None:
        self.refusal = refusal


class _Work:
    """The visit rows as the plan has left them so far."""

    def __init__(self, visits: list[VisitRow], tz_name: str) -> None:
        self.rows: dict[Any, VisitRow] = {v.id: v for v in visits}
        self.tz = tz_name
        self.actions: list = []
        self._new = 0

    def live_rows(self) -> list[VisitRow]:
        return sorted(self.rows.values(), key=lambda v: (_as_utc(v.start_at), str(v.id)))

    def day(self, v: VisitRow) -> date | None:
        return shop_day(v.start_at, self.tz)

    def on(self, day: date | None, *, tech: Any = UNSET, states: tuple | None = None) -> list[VisitRow]:
        out = []
        for v in self.live_rows():
            if self.day(v) != day:
                continue
            if tech is not UNSET and v.tech_id != tech:
                continue
            if states is not None and visit_state(v) not in states:
                continue
            out.append(v)
        return out

    def retire(self, v: VisitRow, reason: str) -> None:
        del self.rows[v.id]
        self.actions.append(Retire(v.id, reason))

    def close(self, v: VisitRow, status: str, reason: str) -> None:
        self.rows[v.id] = replace(v, status=status)
        self.actions.append(Close(v.id, status, reason))

    def reassign(self, v: VisitRow, tech: str | None, reason: str) -> None:
        self.rows[v.id] = replace(v, tech_id=tech)
        self.actions.append(Reassign(v.id, tech, reason))

    def move(self, v: VisitRow, start: datetime, end: datetime, reason: str) -> None:
        self.rows[v.id] = replace(v, start_at=start, end_at=end)
        self.actions.append(Move(v.id, start, end, reason))

    def insert(self, tech: str | None, start: datetime, end: datetime, fields: dict, reason: str) -> None:
        self._new += 1
        key = ("new", self._new)
        self.rows[key] = VisitRow(
            id=key, tech_id=tech, start_at=start, end_at=end, status="scheduled",
            title=fields.get("title"), customer_id=fields.get("customer_id"),
            customer_name=fields.get("customer_name"),
        )
        self.actions.append(Insert(key, tech, start, end, reason))


def _length(v: VisitRow, default_minutes: int) -> timedelta:
    if v.end_at is not None:
        return _as_utc(v.end_at) - _as_utc(v.start_at)
    return timedelta(minutes=default_minutes)


def _finish(work: _Work, finish_day: date | None, reason: str) -> None:
    """F1: close ON SITE and OPEN-on-or-before-the-finish-day; retire later OPEN."""
    for v in work.live_rows():
        state = visit_state(v)
        if state == ON_SITE:
            work.close(v, "completed", reason)
        elif state == OPEN:
            day = work.day(v)
            if finish_day is not None and day is not None and day > finish_day:
                work.retire(v, reason)
            else:
                work.close(v, "completed", reason)


def status_only_arrivals(visits: list[Any]) -> list[Any]:
    """Old rows marked "arrived" with no time: a cancel must ask about them."""
    return [
        v for v in visits
        if is_current(v) and v.arrived_at is None and (v.status or "").lower() == "arrived"
    ]


def _cancel(work: _Work, reason: str) -> None:
    """X1: retire OPEN, close ON SITE as cancelled with its arrival kept."""
    old_rows = status_only_arrivals(work.live_rows())
    if old_rows:
        raise _Refused(Refusal(
            "needs_answer",
            "A visit is marked arrived with no time recorded. On the Appointments "
            "page, enter its arrival time or undo the arrival, then try again.",
            {"question": "status_only_arrival", "visit_ids": [str(v.id) for v in old_rows]},
        ))
    for v in work.live_rows():
        state = visit_state(v)
        if state == OPEN:
            work.retire(v, reason)
        elif state == ON_SITE:
            work.close(v, "cancelled", reason)


def _copy_fields(work: _Work, fields: dict | None) -> None:
    """E1: title and customer onto every OPEN visit. Never inserts or retires."""
    if not fields:
        return
    for v in work.live_rows():
        if visit_state(v) != OPEN or isinstance(v.id, tuple):
            continue
        changes = {
            k: val for k, val in fields.items()
            if k in ("title", "customer_id", "customer_name") and getattr(v, k) != val
        }
        if changes:
            work.rows[v.id] = replace(v, **changes)
            work.actions.append(CopyFields(v.id, changes))


def _refuse(code: str, message: str, **detail: Any) -> None:
    raise _Refused(Refusal(code, message, detail))


_WHERE = "Change it on the Appointments page instead."


def _check_refusals(work: _Work, n: date, target: datetime | None) -> None:
    """R1, R3, R4 (R2 needs the crew change and runs after it)."""
    current = [v for v in work.live_rows() if is_current(v)]
    on_site = [v for v in current if visit_state(v) == ON_SITE]
    if on_site:
        _refuse(
            "crew_on_site",
            "A crew member is on site for this job. Close that day first "
            "(Complete on the Appointments page), then change the date.",
            visit_ids=[str(v.id) for v in on_site],
        )
    if target is None:
        return
    t_day = shop_day(target, work.tz)
    later = sorted({work.day(v) for v in current if work.day(v) != n})
    if later and t_day >= later[0]:
        _refuse(
            "onto_booked_day",
            f"The job is also booked on {later[0].isoformat()}; the date cannot move "
            f"onto or past it. {_WHERE}",
            day=later[0].isoformat(),
        )
    seen: dict[str, int] = {}
    for v in work.on(n, states=(OPEN,)):
        if v.tech_id is not None:
            seen[v.tech_id] = seen.get(v.tech_id, 0) + 1
    doubled = sorted(t for t, c in seen.items() if c > 1)
    if doubled:
        _refuse(
            "two_open_visits",
            f"A technician holds two open visits on {n.isoformat()}; which one should "
            f"move is not clear. {_WHERE}",
            tech_ids=doubled,
        )


def _check_double_book(work: _Work, n: date, target: datetime) -> None:
    """R2, read after the crew change."""
    t_day = shop_day(target, work.tz)
    moving = work.on(n, states=(OPEN,))
    moving_ids = {v.id for v in moving}
    clash = []
    for tech in sorted({v.tech_id for v in moving if v.tech_id is not None}):
        others = [
            v for v in work.on(t_day, tech=tech)
            if is_live(v) and v.id not in moving_ids
        ]
        if others:
            clash.append(tech)
    if clash:
        _refuse(
            "double_booked",
            f"A technician already has a visit on {t_day.isoformat()}. {_WHERE}",
            tech_ids=clash, day=t_day.isoformat(),
        )


def _crew_change(work: _Work, n: date | None, before: list, after: list, fields: dict, minutes: int) -> list:
    """C1–C7, removals first, one tech at a time. Returns the crew it leaves."""
    crew = list(before)
    n_start = None
    if n is not None:
        on_n = [v for v in work.on(n) if is_current(v)]
        n_start = min(_as_utc(v.start_at) for v in on_n) if on_n else None
    for tech in [t for t in before if t not in after]:
        crew.remove(tech)
        if n is None:
            continue
        mine = work.on(n, tech=tech, states=(OPEN,))
        if not mine:
            continue  # C7
        if crew:
            for v in mine:
                work.retire(v, "crew_removed")  # C5
            continue
        # C6: the crew is now empty; the job keeps its booking.
        unassigned_live = [v for v in work.on(n, tech=None) if is_live(v)]
        first, rest = mine[0], mine[1:]
        if unassigned_live:
            work.retire(first, "crew_removed")
        else:
            work.reassign(first, None, "crew_removed")
        for v in rest:
            work.retire(v, "crew_removed")
    for tech in [t for t in after if t not in before]:
        was_empty = not crew
        crew.append(tech)
        if n is None:
            continue  # C4
        if any(is_live(v) for v in work.on(n, tech=tech)):
            continue  # C3
        if was_empty:
            slot = work.on(n, tech=None, states=(OPEN,))
            if slot:
                work.reassign(slot[0], tech, "crew_added")  # C1
                continue
        if n_start is None:
            continue
        work.insert(tech, n_start, n_start + timedelta(minutes=minutes), fields, "crew_added")  # C2
    return crew


def plan_visit_sync(visits: list[VisitRow], edit: VisitEdit) -> VisitPlan:
    """The state table of §5.2a. Pure: reads values, returns actions or a refusal."""
    work = _Work([v for v in visits], edit.tz_name)
    fields = dict(edit.fields or {})
    plan = VisitPlan()
    try:
        _plan(work, edit, fields, plan)
    except _Refused as refused:
        return VisitPlan(refusal=refused.refusal, visits=list(visits))
    plan.actions = work.actions
    plan.visits = work.live_rows()
    return plan


def _plan(work: _Work, edit: VisitEdit, fields: dict, plan: VisitPlan) -> None:
    requested = edit.scheduled_at
    if requested is not UNSET:
        plan.scheduled_at = to_minute(_as_utc(requested)) if requested is not None else None

    if edit.finish_day is not None:
        _finish(work, edit.finish_day, "job_completed")  # F1
        _copy_fields(work, fields)
        return

    if edit.reopen:
        if edit.job_state == "cancelled":
            _cancel(work, "job_reopened")  # R0 applying X1
        else:
            _finish(work, edit.reopen_day, "job_reopened")  # R0 applying F1
        crew = list(edit.crew_before)
        if edit.crew_after is not None:
            crew = _crew_change(work, None, list(edit.crew_before), list(edit.crew_after), fields, edit.duration_minutes)
        if requested is not UNSET and requested is not None:
            _book(work, crew, plan.scheduled_at, fields, edit, reopen=True)
        return

    if edit.cancel:
        _cancel(work, "job_cancelled")  # X1
        _copy_fields(work, fields)
        return

    if edit.job_state in FINISHED_STAGES:
        _copy_fields(work, fields)  # a finished job's edit changes no visit
        return

    stored = to_minute(_as_utc(edit.stored_scheduled_at)) if edit.stored_scheduled_at else None
    date_changed = requested is not UNSET and plan.scheduled_at != stored
    n = current_day(work.live_rows(), work.tz)

    if date_changed and n is not None:
        _check_refusals(work, n, plan.scheduled_at)

    crew = list(edit.crew_before)
    if edit.crew_after is not None:
        crew = _crew_change(work, n, list(edit.crew_before), list(edit.crew_after), fields, edit.duration_minutes)

    if date_changed:
        target = plan.scheduled_at
        if target is None:
            for v in work.live_rows():
                if visit_state(v) == OPEN:
                    work.retire(v, "date_cleared")  # E6
        else:
            n_now = n if n is not None and any(is_current(v) for v in work.on(n)) else None
            if n_now is not None:
                _check_double_book(work, n_now, target)  # R2
                for v in work.on(n_now, states=(OPEN,)):  # E2 (E3: the rest stay)
                    work.move(v, target, target + _length(v, edit.duration_minutes), "date_moved")
            else:
                booked = len(work.actions)
                _book(work, crew, target, fields, edit, reopen=False)  # E4/E5
                if n is not None and len(work.actions) == booked and any(is_current(v) for v in work.live_rows()):
                    # R2, second clause: the crew change emptied N and every
                    # tech left already holds a visit on T (E5), so nothing
                    # lands on T and the job would read a later day instead
                    # of the one typed.
                    t_day = shop_day(target, work.tz)
                    _refuse(
                        "double_booked",
                        f"Everyone left on the job already has a visit on {t_day.isoformat()}. {_WHERE}",
                        tech_ids=[t for t in crew if t is not None], day=t_day.isoformat(),
                    )
    else:
        plan.scheduled_at = UNSET  # unchanged to the minute: E1, the date is not rewritten

    _copy_fields(work, fields)


def _book(work: _Work, crew: list, target: datetime, fields: dict, edit: VisitEdit, *, reopen: bool) -> None:
    """E4/E5: one OPEN visit per crew tech with no Live visit on T."""
    t_day = shop_day(target, work.tz)
    slots = crew or [None]
    closed_here = [t for t in slots if any(is_live(v) for v in work.on(t_day, tech=t))]
    if reopen and closed_here and edit.rebook_closed_day is None:
        raise _Refused(Refusal(
            "needs_answer",
            f"Already has a closed visit on {t_day.isoformat()}. Book that day again?",
            {"question": "rebook_closed_day", "tech_ids": closed_here, "day": t_day.isoformat()},
        ))
    for tech in slots:
        if tech in closed_here and not (reopen and edit.rebook_closed_day):
            continue  # E5
        work.insert(tech, target, target + timedelta(minutes=edit.duration_minutes), fields, "date_set")


# ---------------------------------------------------------------- database


def visit_rows(db: Any, job_id: Any) -> list[Any]:
    from sqlalchemy import select  # noqa: PLC0415

    from gdx_dispatch.models.tenant_models import Appointment  # noqa: PLC0415

    return list(db.execute(
        select(Appointment).where(
            Appointment.job_id == job_id,
            Appointment.deleted_at.is_(None),
        ).order_by(Appointment.start_at, Appointment.id)
    ).scalars().all())


def as_value(appt: Any) -> VisitRow:
    return VisitRow(
        id=appt.id,
        tech_id=appt.tech_id,
        start_at=_as_utc(appt.start_at),
        end_at=_as_utc(appt.end_at) if appt.end_at is not None else None,
        status=appt.status,
        arrived_at=appt.arrived_at,
        title=appt.title,
        customer_id=appt.customer_id,
        customer_name=appt.customer_name,
    )


def job_crew(db: Any, job: Any) -> list[str]:
    """JobAssignment techs, or ``[assigned_to]`` on a legacy job, or []."""
    from sqlalchemy import select  # noqa: PLC0415

    from gdx_dispatch.models.tenant_models import JobAssignment  # noqa: PLC0415

    techs = [
        a.tech_id for a in db.execute(
            select(JobAssignment).where(
                JobAssignment.job_id == str(job.id),
                JobAssignment.deleted_at.is_(None),
            ).order_by(JobAssignment.assigned_at, JobAssignment.id)
        ).scalars().all()
        if a.tech_id
    ]
    if techs:
        return list(dict.fromkeys(str(t) for t in techs))
    return [str(job.assigned_to)] if job.assigned_to else []


def job_visit_fields(db: Any, job: Any) -> dict:
    return visit_fields(db, job.title, job.customer_id)


def visit_fields(db: Any, title: str | None, customer_id: Any) -> dict:
    """What E1 copies onto a visit: the job's title and customer."""
    from gdx_dispatch.models.tenant_models import Customer  # noqa: PLC0415

    if customer_id is not None and not isinstance(customer_id, UUID):
        try:
            customer_id = UUID(str(customer_id))
        except ValueError:
            # Not a uuid: the job write itself refuses it.
            return {"title": (title or "Job")[:300]}
    customer = db.get(Customer, customer_id) if customer_id else None
    customer_name = customer.name if customer is not None else None
    return {
        "title": (title or "Job")[:300],
        "customer_id": customer_id,
        "customer_name": customer_name,
    }


def default_duration(db: Any, job: Any) -> int:
    from gdx_dispatch.routers.appointments import compute_man_hour_duration_minutes  # noqa: PLC0415

    return compute_man_hour_duration_minutes(db, job.id) or 60


def shop_tz(db: Any) -> str:
    from gdx_dispatch.core.pay_periods import shop_tz_name_from_settings  # noqa: PLC0415

    return shop_tz_name_from_settings(db)


def _audit(db: Any, job: Any, user_id: str | None, action: str, details: dict) -> None:
    from gdx_dispatch.core.audit import log_audit_event_sync  # noqa: PLC0415

    log_audit_event_sync(
        db=db,
        tenant_id=str(getattr(job, "company_id", "") or "") or None,
        user_id=user_id or "system",
        action=action,
        entity_type="job",
        entity_id=str(job.id),
        details=details,
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def apply_visit_plan(db: Any, job: Any, plan: VisitPlan, user_id: str | None) -> None:
    """Write a plan's actions and an audit row for each. Flushes, never commits."""
    from gdx_dispatch.models.tenant_models import Appointment  # noqa: PLC0415

    assert plan.refusal is None, "a refused plan has nothing to apply"
    rows = {a.id: a for a in visit_rows(db, job.id)}
    now = datetime.now(UTC)
    planned = {v.id: v for v in plan.visits}
    job_fields: dict | None = None
    for action in plan.actions:
        if isinstance(action, Insert):
            value = planned.get(action.key)
            if value is not None and value.customer_id is not None:
                customer_id, customer_name = value.customer_id, value.customer_name
            else:
                # A save that names no title or customer (a date or crew
                # change) still books the visit for the job's customer.
                if job_fields is None:
                    job_fields = job_visit_fields(db, job)
                customer_id, customer_name = job_fields.get("customer_id"), job_fields.get("customer_name")
            appt = Appointment(
                company_id=job.company_id,
                job_id=job.id,
                customer_id=customer_id,
                customer_name=customer_name,
                tech_id=action.tech_id,
                title=(value.title if value and value.title else (job.title or "Job"))[:300],
                start_at=action.start_at,
                end_at=action.end_at,
                duration_minutes=int((action.end_at - action.start_at).total_seconds() // 60),
                status="scheduled",
                created_by=user_id,
            )
            db.add(appt)
            db.flush()
            # A later action in the same plan can target this row (a tech
            # added on N, then E2 moves N), so it is addressable by its key.
            rows[action.key] = appt
            _audit(db, job, user_id, "visit_added", {
                "visit_id": str(appt.id), "tech_id": action.tech_id,
                "start_at": _iso(action.start_at), "reason": action.reason,
            })
            continue
        appt = rows.get(action.visit_id)
        if appt is None:
            # The plan was made from these rows in this session; an action on
            # a row that is not here would be a silent no-op, so it is an error.
            raise RuntimeError(f"visit plan names a row that is not live: {action.visit_id!r}")
        if isinstance(action, Move):
            details = {
                "visit_id": str(appt.id), "tech_id": appt.tech_id,
                "start_from": _iso(appt.start_at), "start_to": _iso(action.start_at),
                "end_from": _iso(appt.end_at), "end_to": _iso(action.end_at),
                "reason": action.reason,
            }
            appt.start_at = action.start_at
            appt.end_at = action.end_at
            appt.duration_minutes = int((action.end_at - action.start_at).total_seconds() // 60)
            appt.updated_at = now
            _audit(db, job, user_id, "visit_moved", details)
        elif isinstance(action, Reassign):
            details = {
                "visit_id": str(appt.id), "tech_from": appt.tech_id,
                "tech_to": action.tech_id, "start_at": _iso(appt.start_at),
                "reason": action.reason,
            }
            appt.tech_id = action.tech_id
            appt.updated_at = now
            _audit(db, job, user_id, "visit_reassigned", details)
        elif isinstance(action, Retire):
            appt.deleted_at = now
            appt.updated_at = now
            _audit(db, job, user_id, "visit_retired", {
                "visit_id": str(appt.id), "tech_id": appt.tech_id,
                "start_at": _iso(appt.start_at), "reason": action.reason,
            })
        elif isinstance(action, Close):
            details = {
                "visit_id": str(appt.id), "tech_id": appt.tech_id,
                "start_at": _iso(appt.start_at), "status_from": appt.status,
                "status_to": action.status, "arrived_at": _iso(appt.arrived_at),
                "reason": action.reason,
            }
            appt.status = action.status
            if action.status == "completed" and appt.completed_at is None:
                appt.completed_at = now
            appt.updated_at = now
            _audit(db, job, user_id, "visit_closed", details)
        elif isinstance(action, CopyFields):
            changes = {
                k: {"from": str(getattr(appt, k)) if getattr(appt, k) is not None else None,
                    "to": str(v) if v is not None else None}
                for k, v in action.changes.items()
            }
            for k, v in action.changes.items():
                setattr(appt, k, v)
            appt.updated_at = now
            _audit(db, job, user_id, "visit_updated", {"visit_id": str(appt.id), "changes": changes})
    db.flush()


def recompute_job_schedule(db: Any, job: Any, user_id: str | None, reason: str) -> None:
    """Invariant I. Copies the earliest Current visit's start exactly; no-op on
    a finished job or one with no Current visit. Audited when it changes."""
    stage = (getattr(job, "lifecycle_stage", None) or "").lower()
    if stage in FINISHED_STAGES:
        return
    db.flush()
    current = [a for a in visit_rows(db, job.id) if is_current(a)]
    if not current:
        return
    earliest = min(_as_utc(a.start_at) for a in current)
    stored = _as_utc(job.scheduled_at) if job.scheduled_at is not None else None
    if stored == earliest:
        return
    job.scheduled_at = earliest
    _audit(db, job, user_id, "job_schedule_recomputed", {
        "from": _iso(stored), "to": _iso(earliest), "reason": reason,
    })
    db.flush()


def plan_for_job(
    db: Any, job: Any, *, crew_before: list | None = None, **kwargs: Any,
) -> VisitPlan:
    """Read the job's visits and plan an edit against them."""
    kwargs.setdefault("stored_scheduled_at", job.scheduled_at)
    kwargs.setdefault("job_state", _job_state(job))
    edit = VisitEdit(
        tz_name=shop_tz(db),
        crew_before=tuple(crew_before if crew_before is not None else job_crew(db, job)),
        duration_minutes=default_duration(db, job),
        **kwargs,
    )
    return plan_visit_sync([as_value(a) for a in visit_rows(db, job.id)], edit)


def book_new_job(db: Any, job: Any, user_id: str | None) -> None:
    """E4 for a job just created with a date: its crew is booked on that
    date. A job with no visit yet has nothing to refuse; a finished one
    books nothing. Every writer that creates a dated job calls this."""
    if job.scheduled_at is None:
        return
    plan = plan_for_job(
        db, job, stored_scheduled_at=None, scheduled_at=job.scheduled_at,
        fields=job_visit_fields(db, job),
    )
    if plan.refusal is not None:
        raise RuntimeError(f"a new job's visit was refused by the planner: {plan.refusal!r}")
    job.scheduled_at = plan.scheduled_at
    apply_visit_plan(db, job, plan, user_id)
    recompute_job_schedule(db, job, user_id, "job_created")


def _job_state(job: Any) -> str:
    stage = (getattr(job, "lifecycle_stage", None) or "").lower()
    return stage if stage in FINISHED_STAGES else "live"


class VisitRefused(Exception):
    """A refused plan, raised by a writer so its route returns 409."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal.message)
        self.refusal = refusal

    def http_detail(self) -> dict:
        return {"code": self.refusal.code, "message": self.refusal.message, **self.refusal.detail}
