"""Closeout hours pre-fill from today's check-in (Doug, 2026-10-17).

"If they check in and out correctly it should autofill; if not there should
be an option to adjust it." The suggestion rides GET /day-log, which the
closeout sheet already reads; the closeout records whether the closer kept
or changed it. Billing still reads only the submitted ``hours``.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.modules.proposals.models import Estimate

# Before the frozen clock: day_close and closeout_job import these lazily, and
# pydantic building their models under freezegun's FakeDatetime raises.
from gdx_dispatch.routers import labor as _labor  # noqa: F401
from gdx_dispatch.routers import mobile as _mobile  # noqa: F401
from gdx_dispatch.routers.jobs import CloseoutPayload, closeout_job
from gdx_dispatch.services import day_close as dc
from gdx_dispatch.services.visit_sync import shop_instant
from gdx_dispatch.tests import test_day_close_route as _harness
from gdx_dispatch.tests.test_day_close_route import (
    HELPER,
    LEAD,
    TENANT,
    TZ,
    day_log,
    make_job,
    request,
    tech_user,
    technician,
    timer,
    today,
)

db = _harness.db
_flags = _harness._flags

# The suggestion tests pin the shop clock (GDXA-361). The closeout test does
# not: closeout_job imports modules lazily whose pydantic models cannot be
# built under freezegun's FakeDatetime (the same reason test_day_close_closeout
# fails when run on its own).
pinned = pytest.mark.usefixtures("shop_midday_clock")


def _clock(hh: int, mm: int = 0) -> datetime:
    return shop_instant(today(), time(hh, mm), TZ)


def _stop(t, at: datetime, db) -> None:
    """The mobile Stop: closed, 0 minutes, the marker note."""
    t.clock_out = at
    t.duration_minutes = 0
    t.notes = f"{_harness.STOP_NOTE} (elapsed {int((at - dc.aware(t.clock_in)).total_seconds() // 60)} min)"
    db.commit()


@pinned
def test_a_stopped_timer_suggests_check_in_to_check_out(db):
    tl = technician(db, LEAD)
    job = make_job(db)
    t = timer(db, job, LEAD, _clock(9, 10), tech=tl)
    _stop(t, _clock(11, 25), db)

    sug = dc.closeout_hours_suggestion(db, job, LEAD, today(), TZ, now=_clock(15))
    # 2 h 15 min, to the nearest quarter hour; the Stop ended it, not "now".
    assert sug["hours"] == 2.25 and sug["techs"] == 1 and sug["reason"] is None
    assert sug["people"][0]["running"] is False


@pinned
def test_a_running_timer_runs_to_now(db):
    tl = technician(db, LEAD)
    job = make_job(db)
    timer(db, job, LEAD, _clock(8), tech=tl)
    sug = dc.closeout_hours_suggestion(db, job, LEAD, today(), TZ, now=_clock(10, 50))
    assert sug["hours"] == 2.75 and sug["people"][0]["running"] is True


@pinned
def test_crew_counts_each_person_and_uses_the_callers_own_span(db):
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    timer(db, job, LEAD, _clock(8), tech=tl)
    timer(db, job, HELPER, _clock(8, 30), tech=th)
    now = _clock(11)
    # 2.5 h of the lead's 3 h is over SUGGEST_CREW_SHARE: both are crew.
    mine = dc.closeout_hours_suggestion(db, job, HELPER, today(), TZ, now=now)
    assert mine["hours"] == 2.5 and mine["techs"] == 2
    # A desk closer has no timer of their own: the longest span.
    desk = dc.closeout_hours_suggestion(db, job, None, today(), TZ, now=now)
    assert desk["hours"] == 3.0 and desk["techs"] == 2


@pinned
def test_a_helper_who_left_early_is_not_counted_as_crew(db):
    # Audit 2026-10-17: techs multiplies billed labor, so a 30-minute helper
    # must not turn the lead's 4 h into 8 man-hours.
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    timer(db, job, LEAD, _clock(8), tech=tl)
    _stop(timer(db, job, HELPER, _clock(8), tech=th), _clock(8, 30), db)
    sug = dc.closeout_hours_suggestion(db, job, LEAD, today(), TZ, now=_clock(12))
    assert sug["hours"] == 4.0 and sug["techs"] == 1
    assert len(sug["people"]) == 2  # still shown, just not multiplied


@pinned
def test_a_closer_shorter_than_the_crew_gets_no_guess(db):
    # Audit 2026-10-17: the helper's 0.5 h x 2 would bill 1 man-hour of 4.5.
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    timer(db, job, LEAD, _clock(8), tech=tl)
    _stop(timer(db, job, HELPER, _clock(8), tech=th), _clock(8, 30), db)
    sug = dc.closeout_hours_suggestion(db, job, HELPER, today(), TZ, now=_clock(12))
    assert sug["hours"] is None and sug["techs"] is None
    assert sug["reason"] == "shorter_than_crew"
    assert sug["people"][0]["mine"] is False  # the longest first, for the hint


@pinned
def test_a_closer_who_only_tapped_in_gets_no_crew_hours(db):
    # Audit 2026-10-17: a 3-minute tap fell through the tap filter and the
    # closer was handed the lead's 4 h under their own name.
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    timer(db, job, LEAD, _clock(8), tech=tl)
    timer(db, job, HELPER, _clock(11, 57), tech=th)
    sug = dc.closeout_hours_suggestion(db, job, HELPER, today(), TZ, now=_clock(12))
    assert sug["hours"] is None and sug["reason"] == "shorter_than_crew"


@pinned
def test_started_at_is_the_basis_persons_check_in(db):
    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    timer(db, job, HELPER, _clock(7), tech=th)
    timer(db, job, LEAD, _clock(8), tech=tl)
    sug = dc.closeout_hours_suggestion(db, job, LEAD, today(), TZ, now=_clock(11))
    assert sug["started_at"] == dc.aware(_clock(8)).isoformat()


@pinned
def test_a_tap_at_closeout_suggests_nothing(db):
    # Prod 2026-10-17: installs tapped "I'm here" 1–3 min before closing out.
    tl = technician(db, LEAD)
    job = make_job(db)
    timer(db, job, LEAD, _clock(14, 0), tech=tl)
    sug = dc.closeout_hours_suggestion(db, job, LEAD, today(), TZ, now=_clock(14, 3))
    assert sug["hours"] is None and sug["techs"] is None and sug["reason"] == "too_short"


@pinned
def test_a_forgotten_timer_suggests_nothing(db):
    tl = technician(db, LEAD)
    job = make_job(db)
    timer(db, job, LEAD, _clock(0, 5), tech=tl)
    sug = dc.closeout_hours_suggestion(db, job, LEAD, today(), TZ, now=_clock(23, 0))
    assert sug["hours"] is None and sug["reason"] == "too_long"


@pinned
@pytest.mark.parametrize("who", [LEAD, HELPER])
def test_a_timer_the_shift_end_sweep_stopped_suggests_nothing(db, who):
    # The sweep's clock_out is the shift end, not when the tech left: a 2 h
    # visit closed out that evening would pre-fill ~9 h (audit round 4). A
    # swept crew member would inflate the crew the same way.
    from gdx_dispatch.routers.mobile import MOBILE_AUTO_STOP_LABOR_NOTE

    tl, th = technician(db, LEAD), technician(db, HELPER)
    job = make_job(db)
    lead = timer(db, job, LEAD, _clock(8), tech=tl)
    helper = timer(db, job, HELPER, _clock(8), tech=th)
    swept = lead if who == LEAD else helper
    swept.clock_out = _clock(17)
    swept.duration_minutes = 0
    swept.notes = MOBILE_AUTO_STOP_LABOR_NOTE
    db.commit()
    sug = dc.closeout_hours_suggestion(db, job, LEAD, today(), TZ, now=_clock(17, 30))
    assert sug["hours"] is None and sug["techs"] is None and sug["reason"] == "auto_stopped"


@pinned
def test_no_check_in_suggests_nothing(db):
    job = make_job(db)
    sug = dc.closeout_hours_suggestion(db, job, LEAD, today(), TZ)
    assert sug == {"hours": None, "techs": None, "reason": "no_check_in",
                   "started_at": None, "people": []}


@pinned
def test_yesterdays_timer_is_not_todays_check_in(db):
    tl = technician(db, LEAD)
    job = make_job(db)
    timer(db, job, LEAD, _clock(9) - timedelta(days=1), tech=tl)
    sug = dc.closeout_hours_suggestion(db, job, LEAD, today(), TZ, now=_clock(11))
    assert sug["reason"] == "no_check_in"


@pinned
def test_day_log_carries_the_suggestion_and_the_sold_estimate(db):
    tl = technician(db, LEAD)
    job = make_job(db, job_type="Installation")
    timer(db, job, LEAD, _clock(6), tech=tl)
    code, log = day_log(db, job)
    assert code == 200
    assert log["hours_suggestion"]["techs"] == 1
    assert log["hours_suggestion"]["people"][0]["running"] is True
    assert log["accepted_estimate_number"] is None

    db.add(Estimate(
        id=uuid4(), estimate_number="EST-SOLD-1", company_id=TENANT,
        customer_id=job.customer_id, job_id=job.id, status="accepted",
        accepted_at=datetime.now(UTC), public_token=uuid4().hex,
    ))
    db.commit()
    assert day_log(db, job)[1]["accepted_estimate_number"] == "EST-SOLD-1"


@pinned
def test_a_draft_estimate_is_not_a_sold_one(db):
    job = make_job(db, job_type="Installation")
    db.add(Estimate(
        id=uuid4(), estimate_number="EST-DRAFT-1", company_id=TENANT,
        customer_id=job.customer_id, job_id=job.id, status="draft", public_token=uuid4().hex,
    ))
    db.commit()
    assert day_log(db, job)[1]["accepted_estimate_number"] is None


@pytest.mark.parametrize(("source", "suggested", "recorded"), [
    ("check_in", 3.0, "check_in"),
    ("adjusted", 2.5, "adjusted"),
    ("typed", None, "typed"),
    # The server holds the client's label to the figures (audit 2026-10-17):
    ("check_in", 2.5, "adjusted"),   # hours differ from the suggestion
    ("check_in", None, "typed"),     # nothing was suggested
    ("adjusted", 3.0, "check_in"),   # hours equal the suggestion (audit round 4)
])
def test_the_closeout_audit_row_records_where_the_hours_came_from(db, source, suggested, recorded):
    technician(db, LEAD)
    job = make_job(db)
    resp = closeout_job(
        payload=CloseoutPayload(
            parts=[], hours=3.0, no_parts_used=True,
            hours_source=source, hours_suggested=suggested,
            techs_on_site=1, techs_suggested=2,
        ),
        job_id=str(job.id), request=request(), current_user=tech_user(), db=db,
    )
    assert resp.status_code in (200, 201), json.loads(resp.body)
    row = db.execute(
        select(AuditLog).where(AuditLog.action == "job_closeout", AuditLog.entity_id == str(job.id))
    ).scalar_one()
    assert row.details["hours_source"] == recorded
    assert row.details["hours_suggested"] == suggested
    # Crew size multiplies billing: the suggested and submitted both stay.
    assert row.details["techs_suggested"] == 2 and row.details["techs_on_site"] == 1
    # The submitted hours are what billing reads, whatever the source.
    assert row.details["hours"] == 3.0


def test_an_unknown_hours_source_is_refused():
    with pytest.raises(ValueError):
        CloseoutPayload(parts=[], hours=1, hours_source="guessed")
