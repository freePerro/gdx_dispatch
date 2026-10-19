"""GDXA-1939: the revenue forecast, the cash calendar and the Dashboard's
"on the board today" tile each carried their own inline job-stage tuple. The
forecast counted estimate visits and dropped jobs under way; the tile dropped
dated service calls; the forecast valued a job by every estimate on it, drafts
and rejected ones included. All three now read DATED_JOB_STAGES, and the two
forecasts value a job by its accepted, non-deleted estimates.

One dated job per lifecycle stage, real calls on an ORM-built SQLite schema:
a reader that drifts from the shared set selects a different job list here.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from freezegun import freeze_time
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Customer, Job
from gdx_dispatch.modules.forecasting.cash_calendar import cash_calendar
from gdx_dispatch.modules.forecasting.models import ForecastSettings
from gdx_dispatch.modules.forecasting.service import _scheduled_jobs_projection
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.routers import reports
from gdx_dispatch.tests.test_reports import tenant_db_session  # noqa: F401 — fixture
from gdx_dispatch.tests.test_reports_shop_day_window_gdxa538 import _job as _sql_job

TODAY = date(2026, 10, 1)
ALL_STAGES = tuple(Job.__table__.c.lifecycle_stage.type.enums)
EXPECTED = {"scheduled", "service_call", "in_progress"}


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _est(db, job, total, status="accepted", deleted=False):
    db.add(Estimate(job_id=job.id, customer_id=job.customer_id, estimate_number=f"E-{uuid4().hex[:6]}", label="e",
                    proposal_mode=False, total=Decimal(str(total)), status=status, public_token=uuid4().hex,
                    company_id="t", deleted_at=datetime(2026, 9, 1, tzinfo=UTC) if deleted else None))
    db.commit()


def _one_dated_job_per_stage(db):
    c = Customer(id=uuid4(), name="Smith", company_id="t")
    db.add(c)
    db.commit()
    tomorrow_afternoon = datetime.combine(TODAY + timedelta(days=1), datetime.min.time()).replace(hour=15)
    jobs = {}
    for stage in ALL_STAGES:
        j = Job(customer_id=c.id, title=stage, description="t", lifecycle_stage=stage, dispatch_status="assigned",
                company_id="t", assigned_to="tech-1", scheduled_at=tomorrow_afternoon)
        db.add(j)
        db.commit()
        _est(db, j, 1000)
        jobs[stage] = j
    return jobs


def test_forecast_and_cash_calendar_select_the_same_stages(db):
    _one_dated_job_per_stage(db)

    projection = _scheduled_jobs_projection(db, ForecastSettings(scheduled_realization_rate=Decimal("1")), TODAY, 30)
    calendar = cash_calendar(db, days=14, today=TODAY)

    assert {j["title"] for j in projection["jobs"]} == EXPECTED
    assert {r["label"].split(" · ")[-1] for r in calendar["rows"] if r["kind"] == "job"} == EXPECTED


def test_board_tile_counts_the_same_stages(tenant_db_session):  # noqa: F811
    # daily_snapshot bounds its window with ISO strings, so on SQLite the jobs
    # go in as ISO text through raw SQL, as in the GDXA-538 tests; an ORM-written
    # "YYYY-MM-DD HH:MM" sorts before any "YYYY-MM-DDT…" bound of the same day.
    db = tenant_db_session
    noon = "2026-10-02T17:00:00+00:00"
    for stage in ALL_STAGES:
        _sql_job(db, created_at=noon, stage=stage, scheduled_at=noon, assigned_to="tech-1")

    with freeze_time("2026-10-02 18:00:00"):
        snapshot = reports.daily_snapshot(start_date=None, end_date=None, _={}, db=db)

    assert snapshot["assigned_jobs_today"] == len(EXPECTED)


def test_forecast_values_a_job_by_accepted_live_estimates_only(db):
    jobs = _one_dated_job_per_stage(db)
    job = jobs["scheduled"]
    _est(db, job, 9000, status="draft")
    _est(db, job, 9000, status="declined")
    _est(db, job, 9000, deleted=True)
    _est(db, job, 250)  # a second accepted estimate (an add-on) does count

    out = _scheduled_jobs_projection(db, ForecastSettings(scheduled_realization_rate=Decimal("0.5")), TODAY, 30)

    by_title = {j["title"]: j["estimated_value"] for j in out["jobs"]}
    assert by_title == {"scheduled": 1250.0, "service_call": 1000.0, "in_progress": 1000.0}
    assert out["scheduled_total"] == 3250.0
    assert out["expected_total"] == 1625.0
