"""The finishing doors after a "No" (multi-day jobs plan §5.4a): the
closeout's day refusals and ``tapped_at``, the timer pickers skipping day
rows, the 0 h "Yes", /complete and /close-without-work refusing a forgotten
day and auditing before their commit, the suggestion's
``labor_lines``, and update_appointment's round-33 ``visit_arrived``.

The harness is test_day_close_route's; dates are relative to the real shop
today (see that module's docstring).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select

from gdx_dispatch.models.tenant_models import Appointment, Job, JobCloseout, TimeEntry
from gdx_dispatch.routers import jobs as jobs_router
from gdx_dispatch.routers.appointments import AppointmentPatch, update_appointment
from gdx_dispatch.routers.jobs import (
    CloseoutPayload,
    CloseWithoutWorkPayload,
    JobCompletePayload,
    close_job_without_work,
    closeout_billing_suggestion,
    closeout_job,
    complete_job,
)
from gdx_dispatch.services import day_close as dc
from gdx_dispatch.services.visit_sync import shop_day
from gdx_dispatch.tests import test_day_close_route as _harness
from gdx_dispatch.tests.conftest import SHOP_MIDDAY_TZ, SHOP_MIDDAY_UTC
from gdx_dispatch.tests.test_day_close_route import (
    HELPER,
    LEAD,
    TZ,
    _requires_pg,
    at,
    audits,
    billed_man_hours,
    body,
    crew,
    day_close,
    make_job,
    office_user,
    paid,
    request,
    tech_user,
    technician,
    timer,
    today,
    visit,
)

# The shared fixtures, bound by name so pytest collects them here.
db = _harness.db
_flags = _harness._flags

# GDXA-361: the harness puts today's timers at `at(0)`, 00:05 shop time, and a
# close-without-work stamps `now`. In the first five minutes after New York
# midnight the timer was later than the close and stayed a candidate. See
# conftest's shop_midday_clock.
pytestmark = pytest.mark.usefixtures("shop_midday_clock")


def test_the_pinned_clock_puts_today_s_fixtures_in_the_past():
    """The pin has to be in effect, and the latest same-day fixture instant this
    file writes (a stopped timer's clock_out, `at(0) + 1h`) has to be behind it."""
    now = datetime.now(UTC)
    assert SHOP_MIDDAY_UTC <= now < SHOP_MIDDAY_UTC + timedelta(minutes=5), now
    assert TZ == SHOP_MIDDAY_TZ
    assert at(0) + timedelta(hours=1) < now
    assert shop_day(now, TZ) == today()


def closeout(db, job, hours, *, user=None, tapped_at=None):
    resp = closeout_job(
        payload=CloseoutPayload(parts=[], hours=hours, no_parts_used=True, tapped_at=tapped_at),
        job_id=str(job.id), request=request(), current_user=user or tech_user(), db=db,
    )
    return resp.status_code, json.loads(resp.body)


def complete(db, job, user=None):
    resp = complete_job(
        payload=JobCompletePayload(hours=1, no_parts_used=True), job_id=str(job.id),
        request=request(), current_user=user or office_user(), db=db,
    )
    return resp.status_code, json.loads(resp.body)


def close_without_work(db, job, user=None):
    resp = close_job_without_work(
        payload=CloseWithoutWorkPayload(reason="customer cancelled"), job_id=str(job.id),
        request=request(), current_user=user or office_user(), db=db,
    )
    return resp.status_code, json.loads(resp.body)


def suggestion(db, job):
    resp = closeout_billing_suggestion(
        job_id=str(job.id), request=request(), current_user=office_user(), db=db,
    )
    assert resp.status_code == 200, resp.body
    return json.loads(resp.body)


def stage(db, job) -> str:
    db.expire_all()
    return db.get(Job, job.id).lifecycle_stage


def closeouts(db, job) -> int:
    return len(db.execute(select(JobCloseout).where(JobCloseout.job_id == job.id)).scalars().all())


# ------------------------------------------------------ earlier_day_open


def test_yes_refused_while_a_worked_past_day_is_open_and_not_final(db):
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(-1), tech=t, arrived=True)
    visit(db, job, at(0), tech=t, arrived=True)
    first = timer(db, job, LEAD, at(-1), tech=t)

    code, out = closeout(db, job, 8)

    assert code == 409 and out["code"] == "earlier_day_open", out
    assert out["date"] == (today() - timedelta(days=1)).isoformat()
    assert out["detail"].startswith("Close ") and out["detail"].endswith("first: answer No for that day")
    assert closeouts(db, job) == 0 and stage(db, job) == "in_progress"
    assert db.get(TimeEntry, first.id).clock_out is None


def test_single_visit_job_closed_out_the_next_day_is_not_refused(db):
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(-1), tech=t, arrived=True)
    timer(db, job, LEAD, at(-1), tech=t)
    code, out = closeout(db, job, 8)
    assert code == 201, out


def test_early_finish_with_the_next_day_booked_is_not_refused(db):
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(-1), tech=t, arrived=True)
    v_today = visit(db, job, at(0), tech=t, arrived=True)
    visit(db, job, at(1), tech=t)
    timer(db, job, LEAD, at(-1), tech=t)
    timer(db, job, LEAD, at(0), tech=t)
    v1 = db.execute(select(Appointment).where(Appointment.job_id == job.id, Appointment.start_at == at(-1))).scalar_one()
    assert day_close(db, job, body(-1, visits=[v1], people=[(LEAD, 8)]))[0] == 200
    assert v_today is not None
    code, out = closeout(db, job, 6)
    assert code == 201, out


def test_tapped_yesterday_and_replayed_today_is_judged_on_yesterday(db):
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(-1), tech=t, arrived=True)
    v_today = visit(db, job, at(0), tech=t)
    timer(db, job, LEAD, at(-1), tech=t)
    tapped = at(-1, 50)

    # Judged on server now it would be refused (the past day was on site
    # and today's visit is Current); judged on the tap's day it is not.
    assert dc.earlier_day_open(db, job, today(), TZ) is not None
    v_today.arrived_at = at(0)
    v_today.status = "arrived"
    db.commit()

    # Today's crew has arrived, so the replay is later_day_started.
    code, out = closeout(db, job, 8, tapped_at=tapped)
    assert code == 409 and out["code"] == "later_day_started", out
    assert out["date"] == today().isoformat()
    assert out["detail"] == f"Day not recorded: the crew has already started {today().isoformat()}; tell the office."


def test_tapped_yesterday_replayed_before_todays_crew_arrived_closes(db):
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(-1), tech=t, arrived=True)
    visit(db, job, at(0), tech=t, en_route=True)
    timer(db, job, LEAD, at(-1), tech=t)
    code, out = closeout(db, job, 8, tapped_at=at(-1, 50))
    assert code == 201, out


def test_replay_after_start_without_arrival_is_later_day_started(db):
    """Round 34: day 4's crew tapped On my way and Start, not I'm here."""
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    visit(db, job, at(-1), tech=tl, arrived=True)
    visit(db, job, at(0), tech=th, en_route=True)
    timer(db, job, LEAD, at(-1), tech=tl)
    timer(db, job, HELPER, at(0), tech=th)  # Start opens a timer, no arrival
    code, out = closeout(db, job, 8, tapped_at=at(-1, 50))
    assert code == 409 and out["code"] == "later_day_started", out


def test_tapped_at_in_the_future_is_clamped_to_now(db):
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(-1), tech=t, arrived=True)
    visit(db, job, at(0), tech=t, arrived=True)
    timer(db, job, LEAD, at(-1), tech=t)
    code, out = closeout(db, job, 8, tapped_at=at(3))
    assert code == 409 and out["code"] == "earlier_day_open", out


# ----------------------------------------------------------- the pickers


def test_closeout_never_restates_a_day_row(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v1 = visit(db, job, at(-1), tech=t, arrived=True)
    visit(db, job, at(0), tech=t, arrived=True)
    day2 = timer(db, job, LEAD, at(-1), tech=t)
    today_timer = timer(db, job, LEAD, at(0), tech=t)
    assert day_close(db, job, body(-1, visits=[v1], people=[(LEAD, 8)]))[0] == 200

    code, out = closeout(db, job, 6)

    assert code == 201, out
    db.expire_all()
    assert db.get(TimeEntry, day2.id).duration_minutes == 480
    assert db.get(TimeEntry, today_timer.id).duration_minutes == 360
    assert db.get(TimeEntry, today_timer.id).user_id == LEAD
    assert paid(db) == {LEAD: 14.0}
    assert billed_man_hours(db, job) == 8.0


def test_no_then_clock_in_again_then_offline_yes_targets_the_new_timer(db):
    """The lead answers No, clocks in again, taps Yes offline; it replays after
    midnight. The consumed timers are never picked; the new one carries the
    lead's hours."""
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(-1), tech=t, arrived=True)
    first = timer(db, job, LEAD, at(-1), tech=t, stopped=True)
    second = timer(db, job, LEAD, at(-1, 2), tech=t)
    assert day_close(db, job, body(-1, visits=[v], people=[(LEAD, 8)]))[0] == 200
    again = timer(db, job, LEAD, at(-1, 30), tech=t)

    code, out = closeout(db, job, 2, tapped_at=at(-1, 40))

    assert code == 201, out
    db.expire_all()
    assert db.get(TimeEntry, second.id).duration_minutes == 480, "the day row"
    assert db.get(TimeEntry, first.id).duration_minutes == 0, "consumed, never picked"
    assert db.get(TimeEntry, again.id).duration_minutes == 120
    assert db.get(TimeEntry, again.id).user_id == LEAD
    assert paid(db) == {LEAD: 10.0}


def test_a_consumed_stop_marked_timer_is_never_picked(db):
    """With no timer open, the closeout falls to the Stop picker. A timer the
    "No" consumed at 0 still carries the Stop marker and 0 minutes, so only
    ``day_closed_at`` keeps it from being restated as the Yes's row."""
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=t, arrived=True)
    consumed = timer(db, job, LEAD, at(0, 5), tech=t, stopped=True)
    day_row = timer(db, job, LEAD, at(0, 6), tech=t)
    assert day_close(db, job, body(visits=[v], people=[(LEAD, 8)]))[0] == 200

    code, out = closeout(db, job, 2)

    assert code == 201, out
    db.expire_all()
    assert db.get(TimeEntry, consumed.id).duration_minutes == 0
    assert db.get(TimeEntry, day_row.id).duration_minutes == 480
    assert paid(db) == {LEAD: 8.0}, "the 2 h land on an unpaid synthetic row, not the consumed timer"


def test_a_day_row_is_never_restated_as_a_prior_closeout_row(db):
    """The closeout-row picker keys on the note text; a day row whose note
    happens to read the same is still the day's, never restated."""
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=t, arrived=True)
    day_row = timer(db, job, LEAD, at(0), tech=t)
    assert day_close(db, job, body(visits=[v], people=[(LEAD, 8)],
                                   note=jobs_router.CLOSEOUT_LABOR_NOTE))[0] == 200

    code, out = closeout(db, job, 3)

    assert code == 201, out
    db.expire_all()
    assert db.get(TimeEntry, day_row.id).duration_minutes == 480


def test_helper_no_then_lead_yes_same_day(db):
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    crew(db, job, th)
    visit(db, job, at(0), tech=tl, arrived=True)
    lead_timer = timer(db, job, LEAD, at(0), tech=tl)
    timer(db, job, HELPER, at(0, 10), tech=th)
    assert day_close(db, job, body(people=[(HELPER, 4)]), tech_user(HELPER))[0] == 200

    code, out = closeout(db, job, 8)

    assert code == 201, out
    db.expire_all()
    row = db.get(TimeEntry, lead_timer.id)
    assert row.duration_minutes == 480 and row.user_id == LEAD
    assert paid(db) == {LEAD: 8.0, HELPER: 4.0}
    assert billed_man_hours(db, job) == 4.0


def test_helper_no_then_lead_yes_next_morning(db):
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    crew(db, job, th)
    visit(db, job, at(-1), tech=tl, arrived=True)
    lead_timer = timer(db, job, LEAD, at(-1), tech=tl)
    timer(db, job, HELPER, at(-1, 10), tech=th)
    assert day_close(db, job, body(-1, people=[(HELPER, 4)]), tech_user(HELPER))[0] == 200

    code, out = closeout(db, job, 8)

    assert code == 201, out
    db.expire_all()
    assert db.get(TimeEntry, lead_timer.id).duration_minutes == 480
    assert db.get(TimeEntry, lead_timer.id).user_id == LEAD


# ---------------------------------------------------------- the 0 h "Yes"


@pytest.fixture
def hours_required(_flags, monkeypatch):
    flags = dict(_flags, require_hours_on_complete=True)
    monkeypatch.setattr(jobs_router, "_load_workflow_flags", lambda _tid: dict(flags))


def _refused_hours(code, out):
    assert code == 422 and out.get("missing") == ["hours"], (code, out)


def test_zero_hour_yes_after_today_closed_by_no(db, hours_required):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=t, arrived=True)
    timer(db, job, LEAD, at(0), tech=t)
    assert day_close(db, job, body(visits=[v], people=[(LEAD, 6)]))[0] == 200

    code, out = closeout(db, job, 0)

    assert code == 201, out
    assert paid(db) == {LEAD: 6.0}
    assert billed_man_hours(db, job) == 6.0


def test_zero_hour_yes_refused_with_the_callers_timer_running_today(db, hours_required):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(-1), tech=t, arrived=True)
    timer(db, job, LEAD, at(-1), tech=t)
    assert day_close(db, job, body(-1, visits=[v], people=[(LEAD, 8)]))[0] == 200
    today_timer = timer(db, job, LEAD, at(0), tech=t)

    _refused_hours(*closeout(db, job, 0))
    code, out = closeout(db, job, 5)
    assert code == 201, out
    db.expire_all()
    assert db.get(TimeEntry, today_timer.id).duration_minutes == 300
    assert billed_man_hours(db, job) == 8.0


def test_zero_hour_yes_refused_on_a_day_worked_without_a_tap_in(db, hours_required):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(-1), tech=t, arrived=True)
    timer(db, job, LEAD, at(-1), tech=t)
    assert day_close(db, job, body(-1, visits=[v], people=[(LEAD, 8)]))[0] == 200
    _refused_hours(*closeout(db, job, 0))


def test_zero_hour_yes_refused_when_a_helper_taps_in_after_the_no(db, hours_required):
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl, arrived=True)
    timer(db, job, LEAD, at(0), tech=tl)
    assert day_close(db, job, body(visits=[v], people=[(LEAD, 6)]))[0] == 200
    timer(db, job, HELPER, at(0, 30), tech=th)
    _refused_hours(*closeout(db, job, 0))


def test_zero_hour_yes_refused_with_a_person_left_unlisted(db, hours_required):
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl, arrived=True)
    timer(db, job, LEAD, at(0), tech=tl)
    timer(db, job, HELPER, at(0, 10), tech=th)
    assert day_close(db, job, body(visits=[v], people=[(LEAD, 6)]))[0] == 200
    _refused_hours(*closeout(db, job, 0))


def test_zero_hour_yes_refused_with_no_no_anywhere(db, hours_required):
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(0), tech=t, arrived=True)
    timer(db, job, LEAD, at(0), tech=t)
    _refused_hours(*closeout(db, job, 0))


# ---------------------------------------- /complete and close-without-work


@pytest.mark.parametrize("door", [complete, close_without_work])
def test_finish_doors_refuse_a_forgotten_earlier_day(db, door):
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(-1), tech=t, arrived=True)
    visit(db, job, at(0), tech=t, arrived=True)
    forgotten = timer(db, job, LEAD, at(-1), tech=t)

    code, out = door(db, job)

    assert code == 409 and out["code"] == "earlier_day_open", out
    assert stage(db, job) == "in_progress"
    assert db.get(TimeEntry, forgotten.id).clock_out is None
    assert audits(db, job, "job_completed") == audits(db, job, "job_closed_without_work") == []


@pytest.mark.parametrize("door", [complete, close_without_work])
def test_a_single_day_no_show_from_yesterday_still_closes(db, door):
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(-1), tech=t)
    code, out = door(db, job)
    assert code == 200, out
    assert stage(db, job) == "completed"


@pytest.mark.parametrize(("door", "action"), [
    (complete, "job_completed"), (close_without_work, "job_closed_without_work"),
])
def test_finish_doors_audit_inside_their_transaction(db, door, action):
    job = make_job(db)
    visit(db, job, at(0))
    code, _ = door(db, job)
    assert code == 200
    assert len(audits(db, job, action)) == 1


@pytest.mark.parametrize("door", [complete, close_without_work])
def test_a_failed_audit_rolls_the_completion_back(db, door, monkeypatch):
    import gdx_dispatch.core.audit as audit_mod

    job = make_job(db)
    visit(db, job, at(0))

    real = audit_mod.log_audit_event_sync

    def boom(*a, **k):
        if k.get("action") in ("job_completed", "job_closed_without_work"):
            raise RuntimeError("audit store down")
        return real(*a, **k)

    monkeypatch.setattr(audit_mod, "log_audit_event_sync", boom)
    with pytest.raises(HTTPException) as exc:
        door(db, job)
    assert exc.value.status_code == 500
    assert stage(db, job) == "in_progress"


def test_close_without_work_with_day_rows_bills_the_day_rows(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(-1), tech=t, arrived=True)
    timer(db, job, LEAD, at(-1), tech=t)
    assert day_close(db, job, body(-1, visits=[v], people=[(LEAD, 7)]))[0] == 200

    assert suggestion(db, job)["labor_lines"] == [], "nothing offered while in progress"

    code, out = close_without_work(db, job)
    assert code == 200, out
    # No closeout, so the day rows alone are the labor: one line, hours as
    # its quantity, carrying the ids it bills.
    [line] = suggestion(db, job)["labor_lines"]
    assert line["man_hours"] == 7.0
    assert line["quantity"] == 7.0
    assert len(line["time_entry_ids"]) == 1
    assert closeouts(db, job) == 0


def test_close_without_work_leftover_timer_is_not_a_candidate_after_reopen(db):
    th = technician(db, HELPER)
    job = make_job(db)
    timer(db, job, HELPER, at(0), tech=th, stopped=True)
    assert close_without_work(db, job)[0] == 200
    j = db.get(Job, job.id)
    j.lifecycle_stage = "in_progress"
    db.commit()
    visit(db, job, at(1), tech=th)
    assert dc._all_candidates(db, job) == []
    assert dc.earlier_day_open(db, job, today(), TZ) is None


# --------------------------------------------------- round 33: PATCH visit


def _patch(db, v, **fields):
    resp = update_appointment(
        appt_id=v.id, payload=AppointmentPatch(**fields), request=request(),
        user=office_user(), db=db,
    )
    if hasattr(resp, "status_code"):
        return resp.status_code, json.loads(resp.body)
    return 200, resp


def test_moving_an_arrived_visit_to_another_day_is_visit_arrived(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=t, arrived=True)
    code, out = _patch(db, v, start_at=at(1), end_at=at(1) + timedelta(hours=8))
    assert code == 409 and out["code"] == "visit_arrived", out
    assert out["detail"] == "Undo the arrival first" and out["visit_id"] == str(v.id)
    db.expire_all()
    assert db.get(Appointment, v.id).start_at is not None
    assert dc.shop_day(dc.aware(db.get(Appointment, v.id).start_at), TZ) == today()


def test_notes_edit_resending_the_same_start_succeeds(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=t, arrived=True)
    code, out = _patch(db, v, start_at=at(0), end_at=at(0) + timedelta(hours=8), notes="gate code 1234")
    assert code == 200, out


def test_tech_change_on_an_arrived_visit_is_visit_arrived(db):
    # GDXA-383: the arrival is the lead's, and Undo arrival matches a tap only
    # to its own tech's visit, so the visit cannot be handed to the helper.
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl, arrived=True)
    code, out = _patch(db, v, start_at=at(0, 30), end_at=at(0) + timedelta(hours=8), tech_id=th.id)
    assert code == 409 and out["code"] == "visit_arrived", out
    assert "Undo arrival" in out["detail"] and out["visit_id"] == str(v.id)
    db.expire_all()
    assert db.get(Appointment, v.id).tech_id == tl.id


def test_clearing_the_tech_on_an_arrived_visit_is_visit_arrived(db):
    tl = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl, arrived=True)
    code, out = _patch(db, v, tech_id=None)
    assert code == 409 and out["code"] == "visit_arrived", out


def test_tech_change_on_a_status_only_arrival_is_visit_arrived(db):
    # An old arrival with no time is still one Undo arrival can act on.
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl, status="arrived")
    assert v.arrived_at is None
    code, out = _patch(db, v, tech_id=th.id)
    assert code == 409 and out["code"] == "visit_arrived", out
    db.expire_all()
    assert db.get(Appointment, v.id).tech_id == tl.id


def test_tech_change_on_a_completed_visit_with_no_arrival_is_still_allowed(db):
    # Undo arrival refuses it (no_arrival), so a 409 pointing there would be a
    # dead end; with no tap recorded, there is nothing to strand.
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl, status="completed")
    assert v.arrived_at is None
    code, out = _patch(db, v, tech_id=th.id)
    assert code == 200, out
    db.expire_all()
    assert db.get(Appointment, v.id).tech_id == th.id


def test_assigning_a_tech_to_an_unassigned_arrived_visit_is_allowed(db):
    # An office-recorded arrival (no tap) on an unassigned visit; the tapped
    # case, where only the tapper may be named, is in test_arrival_undo.py.
    tl = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=None, arrived=True)
    code, out = _patch(db, v, tech_id=tl.id)
    assert code == 200, out
    db.expire_all()
    assert db.get(Appointment, v.id).tech_id == tl.id


def test_tech_change_on_an_unworked_cancelled_visit_is_still_allowed(db):
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl, status="cancelled")
    code, out = _patch(db, v, tech_id=th.id)
    assert code == 200, out


def test_notes_edit_resending_the_same_tech_on_an_arrived_visit_succeeds(db):
    # The edit form resends every field, tech included.
    tl = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl, arrived=True)
    code, out = _patch(
        db, v, start_at=at(0), end_at=at(0) + timedelta(hours=8), tech_id=tl.id, notes="gate code 1234",
    )
    assert code == 200, out
    db.expire_all()
    assert db.get(Appointment, v.id).notes == "gate code 1234"


def test_tech_change_on_an_unarrived_visit_is_still_allowed(db):
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl)
    code, out = _patch(db, v, tech_id=th.id)
    assert code == 200, out
    db.expire_all()
    assert db.get(Appointment, v.id).tech_id == th.id


def test_moving_an_unarrived_visit_is_still_allowed(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=t)
    code, out = _patch(db, v, start_at=at(1), end_at=at(1) + timedelta(hours=8))
    assert code == 200, out


# --------------------------------------------------------- Postgres only


@_requires_pg
def test_pg_closeout_locks_the_job_before_reading_timers(db):
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(0), tech=t, arrived=True)
    timer(db, job, LEAD, at(0), tech=t)
    seen: list[str] = []
    engine = db.get_bind()

    def capture(_conn, _cursor, statement, *_a):
        seen.append(" ".join(statement.split()).lower())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert closeout(db, job, 4)[0] == 201
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    lock = next(i for i, s in enumerate(seen) if s.startswith("select") and "from jobs" in s and "for update" in s)
    first_timer_read = next(i for i, s in enumerate(seen) if s.startswith("select") and "from time_entries" in s)
    assert lock < first_timer_read, seen[: first_timer_read + 1]


@_requires_pg
def test_pg_no_racing_close_without_work(db):
    import threading

    from sqlalchemy.orm import sessionmaker

    t = technician(db, LEAD)
    job = make_job(db)
    v = visit(db, job, at(0), tech=t, arrived=True)
    tm = timer(db, job, LEAD, at(0), tech=t)
    Session = sessionmaker(bind=db.get_bind())
    results: dict[str, tuple] = {}

    def run(name, fn):
        s = Session()
        try:
            results[name] = fn(s)
        finally:
            s.close()

    threads = [
        threading.Thread(target=run, args=("no", lambda s: day_close(s, job, body(visits=[v], people=[(LEAD, 8)])))),
        threading.Thread(target=run, args=("cww", lambda s: close_without_work(s, job))),
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert results["cww"][0] == 200
    db.expire_all()
    row = db.get(TimeEntry, tm.id)
    if results["no"][0] == 200:
        assert row.duration_minutes == 480 and row.day_closed_at is not None
    else:
        assert results["no"] == (409, results["no"][1]) and results["no"][1]["code"] == "job_finished"
        assert row.duration_minutes == 0

