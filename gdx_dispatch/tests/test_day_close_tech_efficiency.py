"""Multi-day jobs PR 3, tech efficiency (plan §5.4a "Tech efficiency").

The ratio's denominator is time on site, wall-clock: the closeout's
``hours_worked`` plus, per shop day, the job's LONGEST day row — except on the
closeout's own shop day, which counts ``max(hours_worked, longest that day)``
instead of adding on top. The old ``jc.hours_worked > 0`` filter becomes "that
sum > 0".

The report's SQL resolves the lead tech with Postgres-only ``DISTINCT ON``, so
SQLite cannot run it; these tests drive the two Python steps that consume its
per-job rows (``_actual_hours_by_job`` and ``_aggregate``) against real day
rows in a real ORM-built SQLite database. Shop zone: America/New_York (no
settings row).
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.models.tenant_models import TimeEntry
from gdx_dispatch.routers.tech_efficiency import _actual_hours_by_job, _aggregate
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-day-close-eff"
DAY1 = _dt.datetime(2026, 11, 2, 15, 0, tzinfo=_dt.UTC)   # 10:00 New York
DAY2 = DAY1 + _dt.timedelta(days=1)


@pytest.fixture
def db():
    engine = make_fresh_db()
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _row(db, job_id, *, at, minutes, day_closed=True):
    db.add(TimeEntry(
        id=uuid4(), job_id=job_id, tech_id="t", company_id=TENANT, entry_type="job",
        clock_in=at, clock_out=at + _dt.timedelta(minutes=minutes), duration_minutes=minutes,
        day_closed_at=(at + _dt.timedelta(hours=9)) if day_closed else None,
    ))
    db.flush()


def _job(job_id, *, hours, closed_at, sched=10, tech="T1"):
    # The shape the report's SQL hands back per job (SQLite: id as 32 hex).
    return {"job_id": job_id.hex, "tech_id": tech, "tech_name": "Pat",
            "scheduled_hours": sched, "hours_worked": hours,
            "closeout_created_at": closed_at}


def _actual(db, job):
    return _actual_hours_by_job(db, [job])[str(_dt_uuid(job["job_id"]))]


def _dt_uuid(hex_id):
    from uuid import UUID
    return UUID(hex_id)


def test_an_8h_closeout_plus_a_same_day_4h_helper_row_is_8_not_12(db):
    jid = uuid4()
    _row(db, jid, at=DAY2, minutes=240)
    assert _actual(db, _job(jid, hours=8, closed_at=DAY2 + _dt.timedelta(hours=8))) == Decimal("8")


def test_a_same_day_6h_no_plus_a_0h_yes_is_6(db):
    jid = uuid4()
    _row(db, jid, at=DAY2, minutes=360)
    job = _job(jid, hours=0, closed_at=DAY2 + _dt.timedelta(hours=7))
    assert _actual(db, job) == Decimal("6")
    # And the job stays in the report: the old `hours_worked > 0` filter
    # would have dropped it.
    [row] = _aggregate(db, [job])
    assert (row["actual_hours"], row["job_count"], row["efficiency_ratio"]) == (6.0, 1, round(10 / 6, 2))


def test_earlier_days_add_their_longest_row(db):
    jid = uuid4()
    _row(db, jid, at=DAY1, minutes=480)
    _row(db, jid, at=DAY1, minutes=300)   # a second person, same day: not added
    _row(db, jid, at=DAY1, minutes=0)     # a consumed timer
    _row(db, jid, at=DAY1, minutes=900, day_closed=False)  # a plain timer: not a day row
    assert _actual(db, _job(jid, hours=4, closed_at=DAY2 + _dt.timedelta(hours=5))) == Decimal("12")


def test_the_closeout_day_is_the_shop_local_day(db):
    """A closeout at 02:00 UTC on DAY2 is DAY1's evening in New York, so the
    DAY1 row shares its day and counts by max, not on top."""
    jid = uuid4()
    _row(db, jid, at=DAY1, minutes=300)
    closed = DAY2.replace(hour=2)
    assert _actual(db, _job(jid, hours=3, closed_at=closed.isoformat())) == Decimal("5")


def test_a_job_with_no_day_rows_reads_exactly_hours_worked(db):
    jid = uuid4()
    _row(db, jid, at=DAY1, minutes=600, day_closed=False)
    assert _actual(db, _job(jid, hours=Decimal("2.75"), closed_at=DAY1)) == Decimal("2.75")
    # Zero time on site still drops out of the leaderboard.
    assert _aggregate(db, [_job(uuid4(), hours=0, closed_at=DAY1)]) == []


def test_aggregate_sums_per_tech_and_orders_by_ratio(db):
    a, b, c = uuid4(), uuid4(), uuid4()
    _row(db, a, at=DAY1, minutes=240)
    rows = [
        _job(a, hours=4, closed_at=DAY2, sched=10, tech="T1"),   # 4 + 4 = 8
        _job(b, hours=2, closed_at=DAY2, sched=6, tech="T1"),    # 2
        _job(c, hours=5, closed_at=DAY2, sched=10, tech="T2"),   # 5
    ]
    out = _aggregate(db, rows)
    assert [(r["tech_id"], r["scheduled_hours"], r["actual_hours"], r["job_count"]) for r in out] == [
        ("T2", 10.0, 5.0, 1),
        ("T1", 16.0, 10.0, 2),
    ]
    assert [r["efficiency_ratio"] for r in out] == [2.0, 1.6]
