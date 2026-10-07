"""Multi-day jobs PR 3, D13: queued hours (plan §5.4a).

A partial job's board row shows what is still queued:
``effective_duration_hours = max(0, scheduled_duration_hours − worked)``,
where ``worked`` sums, per shop day, the LONGEST day row. The job's hours are
wall-clock, so two techs working 8 h on one day used 8 h of it, not 16.

Over HTTP, on the visit-writers harness (a real ORM-built SQLite database,
shop zone America/New_York because no settings row exists).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from gdx_dispatch.models.tenant_models import TimeEntry
from gdx_dispatch.tests.test_job_visits_routes import (
    client,  # noqa: F401 — the fixture
    env,  # noqa: F401 — its dependency
)
from gdx_dispatch.tests.test_visit_writers import DAY1, DAY2, DAY3, T1, TENANT, _job, _visit


def _day_row(harness, job, *, at, minutes, day_closed=True, user_id=None):
    harness.db.add(TimeEntry(
        id=uuid4(), job_id=job.id, tech_id=T1, company_id=TENANT, entry_type="job",
        user_id=user_id, clock_in=at, clock_out=at + timedelta(minutes=minutes),
        duration_minutes=minutes, day_closed_at=(at + timedelta(hours=9)) if day_closed else None,
    ))
    harness.db.commit()


def _partial_row(client, job):  # noqa: F811
    items = client.client.get("/api/dispatch/partial-jobs").json()["items"]
    return {i["id"]: i for i in items}[str(job.id)]


def test_two_techs_with_8h_each_on_one_day_subtract_8(client):  # noqa: F811
    job = _job(client, crew=(T1,), scheduled_duration_hours=20)
    _visit(client, job, DAY1, status="completed", tech=T1)
    _day_row(client, job, at=DAY1, minutes=480, user_id="tech-user")  # the tech's own row
    _day_row(client, job, at=DAY1, minutes=480)                       # the helper, user_id NULL
    row = _partial_row(client, job)
    assert row["scheduled_duration_hours"] == 20.0
    assert row["effective_duration_hours"] == 12.0


def test_worked_sums_the_longest_row_of_each_shop_day(client):  # noqa: F811
    job = _job(client, crew=(T1,), scheduled_duration_hours=20)
    _visit(client, job, DAY1, status="completed", tech=T1)
    _visit(client, job, DAY2, status="completed", tech=T1)
    _day_row(client, job, at=DAY1, minutes=480)
    _day_row(client, job, at=DAY1, minutes=240)   # a helper who left early
    _day_row(client, job, at=DAY2, minutes=180)
    _day_row(client, job, at=DAY2, minutes=0)     # a consumed timer: not a day row
    _day_row(client, job, at=DAY2, minutes=600, day_closed=False)  # a plain timer: not a day row
    # 01:00 UTC on DAY3 is still DAY2's evening in New York: the same shop
    # day as the 3 h row, so only the longer of the two counts.
    _day_row(client, job, at=DAY3.replace(hour=1), minutes=120)
    row = _partial_row(client, job)
    assert row["effective_duration_hours"] == 20.0 - 8.0 - 3.0


def test_queued_hours_never_go_negative_and_no_estimate_stays_none(client):  # noqa: F811
    over = _job(client, crew=(T1,), scheduled_duration_hours=4)
    _visit(client, over, DAY1, status="completed", tech=T1)
    _day_row(client, over, at=DAY1, minutes=600)
    no_est = _job(client, crew=(T1,))
    _visit(client, no_est, DAY1, status="completed", tech=T1)
    _day_row(client, no_est, at=DAY1, minutes=600)
    assert _partial_row(client, over)["effective_duration_hours"] == 0.0
    no_est_row = _partial_row(client, no_est)
    assert (no_est_row["scheduled_duration_hours"], no_est_row["effective_duration_hours"]) == (None, None)


def test_late_open_and_scheduled_unassigned_read_the_same_queued_hours(client):  # noqa: F811
    past = datetime.now(UTC).replace(hour=15, minute=0, second=0, microsecond=0) - timedelta(days=3)
    late = _job(client, scheduled_at=past, crew=(T1,), scheduled_duration_hours=10)
    _visit(client, late, past)  # still open: late, not partial
    _day_row(client, late, at=past - timedelta(days=1), minutes=240)
    lane_job = _job(client, scheduled_at=DAY3, assigned_to=None, scheduled_duration_hours=6)
    _day_row(client, lane_job, at=DAY1, minutes=90)

    late_rows = {i["id"]: i for i in client.client.get("/api/dispatch/late-open").json()["items"]}
    assert late_rows[str(late.id)]["effective_duration_hours"] == 6.0
    lane = {UUID(i["id"]).hex: i for i in client.client.get("/api/dispatch/scheduled-unassigned").json()["items"]}
    assert (lane[lane_job.id.hex]["scheduled_duration_hours"], lane[lane_job.id.hex]["effective_duration_hours"]) == (6.0, 4.5)
