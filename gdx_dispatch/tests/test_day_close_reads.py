"""The "No" sheet's reads (multi-day jobs plan §5.4a): ``open_day``,
``earlier_day_open``, ``later_day_started``, the latest-finish bound on
candidate timers, and GET /api/jobs/{id}/day-log.

The harness is test_day_close_route's; dates are relative to the real shop
today (see that module's docstring).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gdx_dispatch.models.tenant_models import JobCloseout
from gdx_dispatch.services import day_close as dc
from gdx_dispatch.tests import test_day_close_route as _harness
from gdx_dispatch.tests.test_day_close_route import (
    HELPER,
    LEAD,
    OFFICE,
    TENANT,
    THIRD,
    TZ,
    at,
    body,
    crew,
    day_close,
    day_log,
    make_job,
    office_user,
    tech_user,
    technician,
    timer,
    today,
    visit,
)

# The shared fixtures, bound by name so pytest collects them here.
db = _harness.db
_flags = _harness._flags
from gdx_dispatch.core.audit import log_audit_event_sync


def _open(db, job, user=LEAD):
    return dc.open_day(db, job, user, today(), TZ)


def _earlier(db, job):
    return dc.earlier_day_open(db, job, today(), TZ)


def _finish_audit(db, job):
    """What /complete writes before its commit. ``created_at`` is now (the
    audit table is immutable), which is after every fixture timer."""
    log_audit_event_sync(
        db, tenant_id=TENANT, user_id=OFFICE, action="job_completed",
        entity_type="job", entity_id=str(job.id), details={},
    )
    db.commit()


# -------------------------------------------------------------- open_day


def test_open_day_returns_a_forgotten_arrival_before_today_then_today(db):
    t = technician(db, LEAD)
    job = make_job(db)
    v1 = visit(db, job, at(-1), tech=t, arrived=True)
    visit(db, job, at(0), tech=t, arrived=True)

    od = _open(db, job)
    assert od["date"] == (today() - timedelta(days=1)).isoformat()
    assert [v["id"] for v in od["visits"]] == [str(v1.id)]
    assert od["visits"][0]["state"] == "on_site"

    assert day_close(db, job, body(-1, visits=[v1]))[0] == 200
    assert _open(db, job)["date"] == today().isoformat()


def test_open_day_returns_a_running_timer_with_no_arrival(db):
    t = technician(db, LEAD)
    job = make_job(db)
    timer(db, job, LEAD, at(-2), tech=t)
    visit(db, job, at(0), tech=t)
    od = _open(db, job)
    assert od["date"] == (today() - timedelta(days=2)).isoformat()
    assert od["visits"] == []
    assert [p["user_id"] for p in od["people"]] == [LEAD]


def test_a_rained_out_past_day_does_not_block(db):
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(-1), tech=t)  # booked, never arrived, no timer
    visit(db, job, at(0), tech=t, arrived=True)
    assert _open(db, job)["date"] == today().isoformat()
    assert _earlier(db, job) is None


def test_people_lists_each_user_once_whatever_their_visits(db):
    tl = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(0, 5), tech=tl, arrived=True)
    visit(db, job, at(0, 30), arrived=True)
    timer(db, job, LEAD, at(0, 5), tech=tl, stopped=True)
    timer(db, job, LEAD, at(0, 30), tech=tl)
    assert [p["user_id"] for p in _open(db, job)["people"]] == [LEAD]


def test_desk_open_day_has_no_mine_row(db):
    tl = technician(db, LEAD)
    job = make_job(db)
    timer(db, job, LEAD, at(0), tech=tl)
    assert [p["mine"] for p in _open(db, job, user=OFFICE)["people"]] == [False]


def test_start_without_arrival_then_visit_moved_gives_day_two_with_no_visits(db):
    """A day-2 timer started with Start whose OPEN visit the office moved to
    day 5: open_day is day 2 with no visits and that person; a people-only
    "No" closes it, and then "Yes" is allowed."""
    t = technician(db, LEAD)
    job = make_job(db)
    timer(db, job, LEAD, at(-1), tech=t)
    visit(db, job, at(3), tech=t)  # the moved visit, not arrived

    od = _open(db, job)
    assert od["date"] == (today() - timedelta(days=1)).isoformat()
    assert od["visits"] == [] and [p["user_id"] for p in od["people"]] == [LEAD]
    assert _earlier(db, job) == today() - timedelta(days=1)

    assert day_close(db, job, body(-1, people=[(LEAD, 5)]))[0] == 200
    assert _earlier(db, job) is None


def test_late_tap_in_is_not_in_the_body_and_reopens_the_day(db):
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    v = visit(db, job, at(-1), tech=tl, arrived=True)
    visit(db, job, at(0), tech=tl)
    timer(db, job, LEAD, at(-1), tech=tl)
    payload = body(-1, visits=[v], people=[(LEAD, 8)])
    late = timer(db, job, HELPER, at(-1, 50), tech=th)  # after the sheet was read

    assert day_close(db, job, payload)[0] == 200
    db.expire_all()
    from gdx_dispatch.models.tenant_models import TimeEntry

    assert db.get(TimeEntry, late.id).clock_out is None
    od = _open(db, job)
    assert od["date"] == (today() - timedelta(days=1)).isoformat()
    assert [p["user_id"] for p in od["people"]] == [HELPER]
    assert _earlier(db, job) == today() - timedelta(days=1)


def test_logged_shows_who_closed_whom_and_excludes_added(db):
    """Round 33: the lead's "No" closes U at 4 h; U's sheet shows the 4 h by
    the lead. Round 34: the submitter's added helper is not their logged."""
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    v = visit(db, job, at(0), tech=tl, arrived=True)
    timer(db, job, HELPER, at(0, 10), tech=th)
    assert day_close(db, job, body(visits=[v], people=[(HELPER, 4)], added=[3]))[0] == 200
    timer(db, job, HELPER, at(0, 40), tech=th)  # U taps I'm here again
    timer(db, job, LEAD, at(0, 41), tech=tl)

    people = {p["user_id"]: p for p in _open(db, job, user=HELPER)["people"]}
    assert people[HELPER]["mine"] is True
    assert people[HELPER]["logged"] == [{"hours": 4.0, "closed_by": "Lead Larry"}]
    assert people[LEAD]["logged"] == [], "the added helper's 3 h are not the lead's"


# ------------------------------------------------------- earlier_day_open


def test_single_visit_job_closed_out_the_next_day_is_not_refused(db):
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(-1), tech=t, arrived=True)
    timer(db, job, LEAD, at(-1), tech=t)
    assert _earlier(db, job) is None


def test_worked_past_day_with_a_later_visit_blocks(db):
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(-1), tech=t, arrived=True)
    visit(db, job, at(0), tech=t, arrived=True)
    assert _earlier(db, job) == today() - timedelta(days=1)


def test_past_day_closed_with_a_person_unchecked_blocks(db):
    """Round 33: a "No" closes every visit of a past day with U unchecked."""
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    crew(db, job, th)
    v = visit(db, job, at(-1), tech=tl, arrived=True)
    timer(db, job, LEAD, at(-1), tech=tl)
    timer(db, job, HELPER, at(-1, 10), tech=th)
    assert day_close(db, job, body(-1, visits=[v], people=[(LEAD, 8)]))[0] == 200
    assert _earlier(db, job) == today() - timedelta(days=1)
    assert day_close(db, job, body(-1, people=[(HELPER, 6)]), tech_user(HELPER))[0] == 200
    assert _earlier(db, job) is None


def test_no_visit_a3_timer_from_yesterday_does_not_block(db):
    """Round 34."""
    t = technician(db, LEAD)
    job = make_job(db)
    timer(db, job, LEAD, at(-1), tech=t)
    assert _earlier(db, job) is None


def test_office_completed_only_visit_while_timer_ran_does_not_block(db):
    """Round 34."""
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(-1), tech=t, status="completed", arrived=True)
    timer(db, job, LEAD, at(-1), tech=t)
    assert _earlier(db, job) is None


# ------------------------------------------------------ later_day_started


def test_later_day_started_reads_on_site_visits_and_timers(db):
    t = technician(db, LEAD)
    job = make_job(db)
    visit(db, job, at(0), tech=t)
    yesterday = today() - timedelta(days=1)
    assert dc.later_day_started(db, job, yesterday, TZ) is None, "booked is not started"
    timer(db, job, LEAD, at(0), tech=t)  # Start: no arrival, a timer
    assert dc.later_day_started(db, job, yesterday, TZ) == today()
    assert dc.later_day_started(db, job, today(), TZ) is None


# --------------------------------------------------- latest-finish bound


def test_leftover_timer_from_a_finished_job_is_never_a_candidate_again(db):
    """Round 34/35: a helper's Stop-marked timer left by the finish; the job
    is re-opened and booked a new day."""
    th = technician(db, HELPER)
    job = make_job(db)
    timer(db, job, HELPER, at(-2), tech=th, stopped=True)
    _finish_audit(db, job)
    visit(db, job, at(1), tech=th)

    assert dc._all_candidates(db, job) == []
    assert _open(db, job)["date"] == today().isoformat()
    assert _earlier(db, job) is None


def test_a_closeout_row_is_also_a_finish(db):
    th = technician(db, LEAD)
    job = make_job(db)
    timer(db, job, LEAD, at(-2), tech=th, stopped=True)
    db.add(JobCloseout(job_id=job.id, hours_worked=2, closed_by_user_id=LEAD,
                       closed_at=at(-1), created_at=at(-1)))
    db.commit()
    assert dc._all_candidates(db, job) == []
    fresh = timer(db, job, LEAD, at(0), tech=th)
    assert [t.id for t in dc._all_candidates(db, job)] == [fresh.id]


# --------------------------------------------------------------- day-log


def test_day_log_rows_names_and_totals(db):
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    v1 = visit(db, job, at(-1), tech=tl, arrived=True)
    visit(db, job, at(0), tech=tl, arrived=True)
    timer(db, job, LEAD, at(-1), tech=tl)
    timer(db, job, HELPER, at(-1, 10), tech=th)

    code, log = day_log(db, job)
    assert code == 200
    assert log["rows"] == [] and log["logged_hours_total"] == 0
    assert log["earlier_day_open"] == (today() - timedelta(days=1)).isoformat()

    assert day_close(db, job, body(-1, visits=[v1], people=[(LEAD, 8), (HELPER, 3.5)], added=[2],
                                   note="Frame done"))[0] == 200
    code, log = day_log(db, job)
    assert code == 200
    yday = (today() - timedelta(days=1)).isoformat()
    got = sorted((r["date"], r["person_name"], r["hours"], r["closed_by"], r["note"]) for r in log["rows"])
    assert got == sorted([
        (yday, "Lead Larry", 8.0, "Lead Larry", "Frame done"),
        (yday, "Helper Hank", 3.5, "Lead Larry", "Frame done"),
        (yday, "Added helper", 2.0, "Lead Larry", "Added helper (not tapped in)"),
    ])
    assert log["logged_hours_total"] == 13.5
    assert log["today_has_day_row"] is False
    assert log["earlier_day_open"] is None
    assert log["open_day"]["date"] == today().isoformat()


def test_day_log_today_has_day_row(db):
    t = technician(db, LEAD)
    job = make_job(db)
    timer(db, job, LEAD, at(0), tech=t)
    assert day_close(db, job, body(people=[(LEAD, 2)]))[0] == 200
    assert day_log(db, job)[1]["today_has_day_row"] is True


def test_day_log_outsider_gets_404(db):
    technician(db, THIRD)
    job = make_job(db)
    code, out = day_log(db, job, tech_user(THIRD))
    assert code == 404 and out == {"detail": "job not found"}
    assert day_log(db, job, office_user())[0] == 200


def test_day_log_survives_a_naive_closed_at_in_the_db(db):
    """SQLite hands back naive datetimes; the reads normalise them."""
    t = technician(db, LEAD)
    job = make_job(db)
    timer(db, job, LEAD, at(0), tech=t)
    assert day_close(db, job, body(people=[(LEAD, 1)], closed_at=datetime.now(UTC)))[0] == 200
    assert day_log(db, job)[0] == 200
