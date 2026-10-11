"""GDXA-538: report windows are the shop's days, bounded at its local midnights.

`_resolve_date_range` built every report window from UTC midnights, so on a
Central shop the dashboard's "today" job counts ran 7pm-7pm: an evening job
counted on tomorrow's snapshot and last night's on today's. And
technician-performance counted a job completed only when the legacy `status`
varchar read exactly 'Complete', missing 'Completed' and NULL (imported) rows.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from freezegun import freeze_time
from sqlalchemy import text

from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.routers import reports
from gdx_dispatch.tests.test_reports import tenant_db_session  # noqa: F401 — fixture

# 9:30pm Central on 2026-09-14 (CDT, UTC-5). The UTC date is already the 15th.
EVENING_UTC = "2026-09-15 02:30:00"


def _set_tz(db, tz_name: str) -> None:
    db.add(AppSettings(timezone=tz_name))
    db.commit()
    db.info.pop("gdx_shop_timezone", None)


def _job(db, *, created_at: str, stage: str = "scheduled", status: str | None = None,
         completed_at: str | None = None, scheduled_at: str | None = None,
         assigned_to: str | None = None) -> str:
    job_id = str(uuid.uuid4())
    db.execute(
        text(
            """
            INSERT INTO jobs (id, lifecycle_stage, status, created_at, completed_at,
                              scheduled_at, assigned_to, deleted_at)
            VALUES (:id, :stage, :status, :created_at, :completed_at,
                    :scheduled_at, :assigned_to, NULL)
            """
        ),
        {"id": job_id, "stage": stage, "status": status, "created_at": created_at,
         "completed_at": completed_at, "scheduled_at": scheduled_at, "assigned_to": assigned_to},
    )
    db.commit()
    return job_id


def _invoice(db, *, job_id: str, total: float, created_at: str) -> None:
    db.execute(
        text(
            """
            INSERT INTO invoices (id, job_id, total, status, created_at, invoice_date, deleted_at)
            VALUES (:id, :job_id, :total, 'sent', :created_at, :invoice_date, NULL)
            """
        ),
        {"id": str(uuid.uuid4()), "job_id": job_id, "total": total,
         "created_at": created_at, "invoice_date": created_at[:10]},
    )
    db.commit()


def test_window_is_bounded_at_the_shops_local_midnights(tenant_db_session):  # noqa: F811
    _set_tz(tenant_db_session, "America/Chicago")
    w = reports._resolve_date_range(tenant_db_session, "2026-09-14", "2026-09-14")
    assert w.start_ts == "2026-09-14T05:00:00+00:00"
    assert w.end_ts == "2026-09-15T05:00:00+00:00"
    assert (w.start_d, w.end_d, w.end_date) == (date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 14))


def test_window_dates_are_not_sliced_from_the_timestamp_east_of_utc(tenant_db_session):  # noqa: F811
    # East of UTC the local midnight falls on the PREVIOUS UTC date; a date
    # recovered by slicing start_ts[:10] would be a day early.
    _set_tz(tenant_db_session, "Europe/Berlin")
    w = reports._resolve_date_range(tenant_db_session, "2026-09-14", "2026-09-14")
    assert w.start_ts == "2026-09-13T22:00:00+00:00"
    assert w.start_d == date(2026, 9, 14)


def test_window_crosses_a_dst_change_on_local_midnights(tenant_db_session):  # noqa: F811
    # CDT ends 2026-11-01: the start is UTC-5, the end UTC-6.
    _set_tz(tenant_db_session, "America/Chicago")
    w = reports._resolve_date_range(tenant_db_session, "2026-10-31", "2026-11-01")
    assert w.start_ts == "2026-10-31T05:00:00+00:00"
    assert w.end_ts == "2026-11-02T06:00:00+00:00"


def test_default_window_ends_on_the_shops_today_not_utcs(tenant_db_session):  # noqa: F811
    _set_tz(tenant_db_session, "America/Chicago")
    with freeze_time(EVENING_UTC):
        w = reports._resolve_date_range(tenant_db_session, None, None)
    assert w.end_date == date(2026, 9, 14)
    assert w.start_d == date(2026, 8, 16)


def test_daily_snapshot_counts_the_evening_and_not_last_night(tenant_db_session):  # noqa: F811
    db = tenant_db_session
    _set_tz(db, "America/Chicago")
    # 9:00pm Central on the 14th: today's, though its UTC date is the 15th.
    evening = "2026-09-15T02:00:00+00:00"
    _job(db, created_at=evening, stage="completed", status=None, completed_at=evening)
    _job(db, created_at=evening, stage="completed", completed_at=evening)
    _job(db, created_at=evening, stage="scheduled", scheduled_at=evening, assigned_to="tech-1")
    # 11:00pm Central on the 13th: yesterday's, though its UTC date is the 14th.
    # Deliberately unequal to the evening set, so UTC bounds cannot pass by
    # trading one for the other (they would read 1 / 1 / 0).
    last_night = "2026-09-14T04:00:00+00:00"
    _job(db, created_at=last_night, stage="completed", completed_at=last_night)

    with freeze_time(EVENING_UTC):
        out = reports.daily_snapshot(start_date=None, end_date=None, _={}, db=db)

    assert out["snapshot_date"] == "2026-09-14"
    assert out["new_jobs_today"] == 3
    assert out["jobs_completed_today"] == 2
    assert out["assigned_jobs_today"] == 1


def test_summary_jobs_completed_uses_local_bounds(tenant_db_session):  # noqa: F811
    db = tenant_db_session
    _set_tz(db, "America/Chicago")
    evening = "2026-09-15T02:00:00+00:00"  # 9pm Central on the 14th
    _job(db, created_at=evening, stage="completed", completed_at=evening)

    out = reports.reports_summary(start_date="2026-09-14", end_date="2026-09-14", _={}, db=db)
    assert out["jobs_completed"] == 1
    out = reports.reports_summary(start_date="2026-09-15", end_date="2026-09-15", _={}, db=db)
    assert out["jobs_completed"] == 0


def test_credits_stay_on_the_ledgers_utc_day(tenant_db_session):  # noqa: F811
    # The GL posts a credit at created_at.date() (UTC) and the per-period
    # queries bucket it by date_trunc on that UTC timestamp. A credit at 8pm
    # Central on Sep 30 is the ledger's Oct 1: shop-midnight edges would net it
    # into September while it buckets into October.
    db = tenant_db_session
    _set_tz(db, "America/Chicago")
    w = reports._resolve_date_range(db, "2026-09-30", "2026-09-30")
    assert reports._ledger_edges(w) == ("2026-09-30T00:00:00+00:00", "2026-10-01T00:00:00+00:00")

    invoice_id = str(uuid.uuid4())
    db.execute(
        text(
            "INSERT INTO invoices (id, total, status, created_at, invoice_date, deleted_at) "
            "VALUES (:id, 100, 'sent', '2026-09-30T17:00:00+00:00', '2026-09-30', NULL)"
        ),
        {"id": invoice_id},
    )
    db.execute(
        text(
            "INSERT INTO invoice_adjustments (id, invoice_id, kind, amount, created_at) "
            "VALUES (:id, :inv, 'credit_memo', 25, '2026-10-01T01:00:00+00:00')"
        ),
        {"id": str(uuid.uuid4()), "inv": invoice_id},
    )
    db.commit()

    sept = reports.reports_summary(start_date="2026-09-30", end_date="2026-09-30", _={}, db=db)
    octo = reports.reports_summary(start_date="2026-10-01", end_date="2026-10-01", _={}, db=db)
    assert sept["revenue_total"] == pytest.approx(100.0)
    assert octo["revenue_total"] == pytest.approx(-25.0)


def test_technician_performance_counts_every_completed_job_once(tenant_db_session):  # noqa: F811
    db = tenant_db_session
    _set_tz(db, "America/Chicago")
    tech = str(uuid.uuid4())
    db.execute(text("INSERT INTO technicians (id, name, deleted_at) VALUES (:id, 'Tess', NULL)"), {"id": tech})
    db.commit()
    noon = "2026-09-14T17:00:00+00:00"
    complete = _job(db, created_at=noon, stage="completed", status="Complete", completed_at=noon, assigned_to=tech)
    completed = _job(db, created_at=noon, stage="completed", status="Completed", completed_at=noon, assigned_to=tech)
    imported = _job(db, created_at=noon, stage="completed", status=None, completed_at=noon, assigned_to=tech)
    open_job = _job(db, created_at=noon, stage="scheduled", status="Complete", assigned_to=tech)
    # Two invoices on one job: revenue is each invoice once, the job counted once.
    _invoice(db, job_id=complete, total=100, created_at=noon)
    _invoice(db, job_id=complete, total=50, created_at=noon)
    _invoice(db, job_id=completed, total=200, created_at=noon)
    _invoice(db, job_id=imported, total=300, created_at=noon)
    _invoice(db, job_id=open_job, total=7, created_at=noon)

    items = reports.technician_performance(
        start_date="2026-09-14", end_date="2026-09-14", _={}, db=db
    )["items"]

    assert len(items) == 1
    # A stale 'Complete' status on a job whose lifecycle is not completed
    # does not count; the three completed jobs do, whatever their status says.
    assert items[0]["jobs_completed"] == 3
    assert items[0]["revenue"] == pytest.approx(657.0)
