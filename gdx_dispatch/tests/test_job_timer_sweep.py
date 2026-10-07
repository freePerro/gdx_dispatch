"""tasks/job_timer_sweep.py — a job timer stops at the tech's shift end (D14).

The sweep runs the real UPDATE and the real hash-chained audit writer against
the day-close harness's ORM-built schema. What it must never do is write
hours: the timer stops at 0 h exactly as the phone's Stop does, with Stop's
marker leading the note, so that tech's "No" (or the closeout) still finds it.

Fixed-calendar tests pass ``now`` explicitly; the day-close tests use the
harness's real-today dates, because the "No" and the closeout read server time.
Shop zone is the harness's ``America/New_York`` (no AppSettings row), so the
shop default shift end is 17:00.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.models.tenant_models import Job, TimeEntry, User
from gdx_dispatch.routers.mobile import MOBILE_STOP_LABOR_NOTE, _shift_end_state
from gdx_dispatch.services import day_close as dc
from gdx_dispatch.services.visit_sync import shop_instant
from gdx_dispatch.tasks.job_timer_sweep import (
    AUTO_STOP_ACTION,
    stop_timers_past_shift_end,
)
from gdx_dispatch.tests import test_day_close_route as _harness
from gdx_dispatch.tests.test_day_close_closeout import close_without_work, closeout
from gdx_dispatch.tests.test_day_close_route import (
    LEAD,
    TENANT,
    TZ,
    at,
    body,
    day_close,
    make_job,
    paid,
    technician,
    timer,
    today,
    visit,
)

db = _harness.db
_flags = _harness._flags

MONDAY = date(2026, 10, 5)
SATURDAY = date(2026, 10, 3)


def local(day: date, hh: int, mm: int = 0) -> datetime:
    return shop_instant(day, time(hh, mm), TZ)


def sweep(db, now: datetime | None = None) -> dict:
    return stop_timers_past_shift_end(db, TENANT, now=now)


def row(db, entry) -> TimeEntry:
    db.expire_all()
    return db.get(TimeEntry, entry.id)


def aware(v):
    return v.replace(tzinfo=UTC) if v is not None and v.tzinfo is None else v


def auto_stop_audits(db) -> list[AuditLog]:
    return list(db.execute(
        select(AuditLog).where(AuditLog.action == AUTO_STOP_ACTION).order_by(AuditLog.id)
    ).scalars().all())


def assert_stopped_at_zero(t: TimeEntry, at_: datetime) -> None:
    assert aware(t.clock_out) == at_
    assert t.duration_minutes == 0
    assert t.notes == f"{MOBILE_STOP_LABOR_NOTE} — auto-stopped at shift end"
    # Every reader of a stopped timer tests this prefix.
    assert dc.is_candidate(t, MOBILE_STOP_LABOR_NOTE)


# --------------------------------------------------------- the window edges


def test_a_minute_before_shift_end_is_left_running_and_shift_end_stops_it(db):
    job = make_job(db)
    t = timer(db, job, LEAD, local(MONDAY, 8))

    assert sweep(db, now=local(MONDAY, 16, 59)) == {"stopped": 0, "failures": 0}
    assert row(db, t).clock_out is None

    assert sweep(db, now=local(MONDAY, 17)) == {"stopped": 1, "failures": 0}
    assert_stopped_at_zero(row(db, t), local(MONDAY, 17))


def test_a_late_sweep_still_stops_at_the_shift_end_not_at_the_beat(db):
    job = make_job(db)
    t = timer(db, job, LEAD, local(MONDAY, 8))
    sweep(db, now=local(MONDAY, 17, 14))
    assert aware(row(db, t).clock_out) == local(MONDAY, 17)


def test_the_users_own_shift_end_wins_over_the_shop_default(db):
    user = db.get(User, UUID(LEAD))
    user.shift_end = time(16, 30)
    db.commit()
    job = make_job(db)
    t = timer(db, job, LEAD, local(MONDAY, 8))

    sweep(db, now=local(MONDAY, 16, 29))
    assert row(db, t).clock_out is None
    sweep(db, now=local(MONDAY, 16, 30))
    assert_stopped_at_zero(row(db, t), local(MONDAY, 16, 30))


def test_a_saturday_timer_stops_at_the_same_shift_end(db):
    # The workdays mask (default Mon-Fri) decides only the warning.
    job = make_job(db)
    t = timer(db, job, LEAD, local(SATURDAY, 9))
    sweep(db, now=local(SATURDAY, 16, 59))
    assert row(db, t).clock_out is None
    sweep(db, now=local(SATURDAY, 17))
    assert_stopped_at_zero(row(db, t), local(SATURDAY, 17))


def test_a_timer_started_after_shift_end_runs_until_shop_midnight(db):
    job = make_job(db)
    late = timer(db, job, LEAD, local(MONDAY, 17))  # at the shift end = late work
    later = timer(db, job, LEAD, local(MONDAY, 19, 30))

    assert sweep(db, now=local(MONDAY, 23, 59)) == {"stopped": 0, "failures": 0}
    assert row(db, late).clock_out is None and row(db, later).clock_out is None

    midnight = local(MONDAY + timedelta(days=1), 0)
    assert sweep(db, now=midnight) == {"stopped": 2, "failures": 0}
    assert_stopped_at_zero(row(db, late), midnight)
    assert_stopped_at_zero(row(db, later), midnight)
    reasons = {a.details["reason"] for a in auto_stop_audits(db)}
    assert reasons == {"shop_midnight"}


def test_an_already_stopped_timer_is_left_alone(db):
    job = make_job(db)
    t = timer(db, job, LEAD, local(MONDAY, 8), stopped=True)
    before = (row(db, t).clock_out, row(db, t).notes)

    assert sweep(db, now=local(MONDAY + timedelta(days=2), 12)) == {"stopped": 0, "failures": 0}
    after = row(db, t)
    assert (after.clock_out, after.notes) == before
    assert auto_stop_audits(db) == []


def test_a_deleted_or_userless_open_timer_is_left_alone(db):
    job = make_job(db)
    gone = timer(db, job, LEAD, local(MONDAY, 8))
    gone_row = row(db, gone)
    gone_row.deleted_at = local(MONDAY, 9)
    office = TimeEntry(
        id=uuid4(), company_id=TENANT, job_id=job.id, tech_id="office", user_id=None,
        clock_in=local(MONDAY, 8), entry_type="job", created_at=local(MONDAY, 8),
    )
    db.add(office)
    db.commit()

    assert sweep(db, now=local(MONDAY, 18)) == {"stopped": 0, "failures": 0}
    assert row(db, gone).clock_out is None and row(db, office).clock_out is None


def test_the_audit_row_is_one_per_timer_by_system_and_writes_no_hours(db):
    job = make_job(db)
    t = timer(db, job, LEAD, local(MONDAY, 8))

    sweep(db, now=local(MONDAY, 17, 5))
    sweep(db, now=local(MONDAY, 17, 20))  # the next beat finds nothing to do

    audits = auto_stop_audits(db)
    assert len(audits) == 1
    a = audits[0]
    assert a.user_id == "system"
    assert a.entity_type == "job" and a.entity_id == str(job.id)
    assert a.details["entry_id"] == str(t.id)
    assert a.details["user_id"] == LEAD
    assert a.details["reason"] == "shift_end"
    assert a.details["stopped_at"] == local(MONDAY, 17).isoformat()
    assert a.details["elapsed_minutes"] == 9 * 60
    assert a.details["recorded_minutes"] == 0
    # Payroll reads its own query: nothing is paid for an auto-stopped timer.
    assert paid(db) == {}


# ------------------------------------------ the day still closes by "No"


def test_auto_stop_then_that_techs_no_answers_200_and_closes_the_day(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(-1), tech=t, arrived=True)
    tm = timer(db, job, LEAD, at(-1), tech=t)

    assert sweep(db)["stopped"] == 1
    assert row(db, tm).clock_out is not None

    code, out = day_close(db, job, body(-1, visits=[v], people=[(LEAD, 8)]))

    assert code == 200, out
    assert row(db, tm).day_closed_at is not None
    assert paid(db) == {LEAD: 8.0}


def test_auto_stop_then_yes_on_a_later_day_is_refused_until_that_no(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v1 = visit(db, job, at(-1), tech=t, arrived=True)
    visit(db, job, at(0), tech=t, arrived=True)
    timer(db, job, LEAD, at(-1), tech=t)

    assert sweep(db)["stopped"] == 1

    code, out = closeout(db, job, 6)
    assert code == 409 and out["code"] == "earlier_day_open", out
    assert out["date"] == (today() - timedelta(days=1)).isoformat()

    assert day_close(db, job, body(-1, visits=[v1], people=[(LEAD, 8)]))[0] == 200
    code, out = closeout(db, job, 6)
    assert code == 201, out


def test_auto_stop_then_next_day_yes_on_a_single_visit_job_pays_the_tech(db):
    """The sweep must not change who a next-morning "Yes" pays.

    Before the sweep the forgotten timer was still open, and the closeout
    restated it at any age. Once the sweep closes it, the closeout finds it as
    a stopped row, and a hand Stop's row is only restated within 24 h of its
    clock_in. Held to that window, this "Yes" would post the attested hours to
    the user-less row and payroll would pay the tech nothing.
    """
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(-1), tech=t, arrived=True)
    timer(db, job, LEAD, at(-1), tech=t)

    assert sweep(db)["stopped"] == 1

    code, out = closeout(db, job, 6)
    assert code == 201, out
    rows = db.execute(
        select(TimeEntry.user_id, TimeEntry.duration_minutes).where(TimeEntry.job_id == job.id)
    ).all()
    assert [(str(r.user_id), r.duration_minutes) for r in rows] == [(LEAD, 360)]
    assert paid(db) == {LEAD: 6.0}


def test_a_swept_timer_from_before_a_finish_is_not_restated_after_a_reopen(db):
    """Without the sweep, the finish closes the open timer and a re-opened
    job's "Yes" never restates it. A row the sweep already closed must not be
    restated either, or today's hours post to that old clock_in's pay period.
    """
    t = technician(db, LEAD)
    job = make_job(db)
    old = timer(db, job, LEAD, at(-10), tech=t)
    assert sweep(db)["stopped"] == 1

    assert close_without_work(db, job)[0] == 200
    j = db.get(Job, job.id)
    j.lifecycle_stage = "in_progress"
    db.commit()

    code, out = closeout(db, job, 6)
    assert code == 201, out
    db.refresh(old)
    assert old.duration_minutes == 0, "the old swept row is never restated"
    assert paid(db) == {}


# ---------------------------------------------- what the phone is given


def test_the_phone_gets_the_effective_shift_end_and_whether_today_is_a_workday(db):
    user = db.get(User, UUID(LEAD))
    user.shift_end = time(16, 30)
    db.commit()

    monday = _shift_end_state(db, LEAD, local(MONDAY, 16))
    assert monday == {
        "end_at": local(MONDAY, 16, 30).isoformat(),
        "end_label": "4:30 PM",
        "workday": True,
        "warn_minutes": 30,
    }
    saturday = _shift_end_state(db, LEAD, local(SATURDAY, 16))
    assert saturday["workday"] is False
    assert saturday["end_at"] == local(SATURDAY, 16, 30).isoformat()


def test_the_phone_falls_back_to_the_shop_default_shift_end(db):
    state = _shift_end_state(db, LEAD, local(MONDAY, 12))
    assert state["end_label"] == "5:00 PM"
    assert state["end_at"] == local(MONDAY, 17).isoformat()
