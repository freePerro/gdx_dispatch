"""tools/stale_job_timer_repair.py — closing what was left running.

Four per-job timers on prod have been open 85-118 days (opened 2026-04-29,
05-04, 05-21, 06-01), all from before ``73bf873`` taught closeout to end the
arrival timer. They are not a live leak; they are residue. This tool closes
them.

The whole risk of a tool like this is the temptation to salvage the elapsed
span. Three months of wall clock is ~2,800 hours per row, and
``time_entries.duration_minutes`` IS payroll hours (payroll.py:248). Every
test here exists to prove the tool refuses that salvage, and that it cannot
reach a timer belonging to a tech who is still on the job.
"""
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import stale_job_timer_repair as repair  # noqa: E402

TENANT = "tenant-1"


@pytest.fixture
def db():
    # See test_mobile_job_clock.py — same reason. This tool writes
    # `CAST(id AS TEXT) = :id`, chosen precisely so it works on both planes,
    # and a SQLite-only proof would not have caught the `::text` it replaced.
    pg_url = os.environ.get("GDX_PROOF_PG_URL")
    if pg_url:
        engine = create_engine(pg_url)
        # Not drop_all: the model graph has a circular FK (estimates <->
        # proposal_tiers) that SQLAlchemy cannot topologically sort for DROP.
        # Resetting the schema sidesteps the cycle entirely.
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    else:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()
    engine.dispose()


def _job(db) -> str:
    """A real jobs row. Postgres enforces time_entries.job_id -> jobs.id;
    SQLite does not, so a SQLite-only fixture happily inserted orphans."""
    customer_id, job_id = str(uuid4()), str(uuid4())
    db.execute(text(
        "INSERT INTO customers (id, name, company_id) VALUES (:id, 'Acme', :co)"
    ), {"id": customer_id, "co": TENANT})
    db.execute(text(
        "INSERT INTO jobs (id, company_id, customer_id, title, dispatch_status, created_at)"
        " VALUES (:id, :co, :cust, 'Door repair', 'on_site', :now)"
    ), {"id": job_id, "co": TENANT, "cust": customer_id, "now": datetime.now(UTC)})
    db.commit()
    return job_id


def _timer(db, *, days_open: float, entry_type: str = "job", clock_out=None) -> str:
    entry_id = str(uuid4())
    db.execute(text(
        "INSERT INTO time_entries (id, company_id, job_id, tech_id, user_id, clock_in,"
        "                          clock_out, duration_minutes, entry_type, created_at)"
        " VALUES (:id, :co, :job, 'tech-1', 'user-michael', :ci, :out, NULL, :et, :now)"
    ), {
        "id": entry_id, "co": TENANT, "job": _job(db),
        "ci": datetime.now(UTC) - timedelta(days=days_open),
        "out": clock_out, "et": entry_type, "now": datetime.now(UTC),
    })
    db.commit()
    return entry_id


def _row(db, entry_id: str) -> dict:
    return dict(db.execute(
        text("SELECT clock_out, duration_minutes, hourly_rate, notes"
             " FROM time_entries WHERE id = :id"),
        {"id": entry_id},
    ).mappings().first())


def test_finds_only_timers_past_the_age_floor(db):
    """A tech still on the job today must never be clocked out by a cleanup."""
    old = _timer(db, days_open=118)
    _timer(db, days_open=0.2)  # someone working right now

    stale = repair.fetch_stale(db, min_age_days=7)
    assert [s.entry_id for s in stale] == [old]


def test_ignores_the_day_shift_clock(db):
    """Shift rows have their own sweep (tasks/timeclock_sweep.py)."""
    _timer(db, days_open=90, entry_type="day")
    assert repair.fetch_stale(db, min_age_days=7) == []


def test_ignores_already_closed_timers(db):
    _timer(db, days_open=90, clock_out=datetime.now(UTC))
    assert repair.fetch_stale(db, min_age_days=7) == []


def test_closes_at_zero_minutes_never_the_elapsed_span(db):
    """The one that matters.

    Counterfactual: change the tool's ``duration_minutes = 0`` to the elapsed
    value and this fails with 129600 != 0 — 2,160 hours banked on one row.
    """
    entry_id = _timer(db, days_open=90)
    stale = repair.fetch_stale(db, min_age_days=7)
    repair.apply_plan(db, stale, operator="test")

    row = _row(db, entry_id)
    assert row["clock_out"] is not None, "the timer must stop claiming to be live"
    assert row["duration_minutes"] == 0, (
        f"banked {row['duration_minutes']} payable minutes of unattended clock"
    )
    assert row["hourly_rate"] is None
    # The span survives where a human reads it.
    assert repair.REPAIR_NOTE in row["notes"]
    assert "129600 min" in row["notes"]


def test_adds_no_payroll_hours(db):
    """Asserted through payroll's own reader, not through the column."""
    from gdx_dispatch.routers.payroll import _fetch_tech_hours

    _timer(db, days_open=90)
    repair.apply_plan(db, repair.fetch_stale(db, min_age_days=7), operator="test")

    by_tech = _fetch_tech_hours(
        db,
        tenant_id=TENANT,
        start=(datetime.now(UTC) - timedelta(days=365)).date(),
        end=(datetime.now(UTC) + timedelta(days=1)).date(),
    )
    assert sum(sum(d.values()) for d in by_tech.values()) == 0.0


def test_is_rerunnable(db):
    entry_id = _timer(db, days_open=90)
    repair.apply_plan(db, repair.fetch_stale(db, min_age_days=7), operator="test")
    first = _row(db, entry_id)

    # Second pass sees nothing left to do.
    assert repair.fetch_stale(db, min_age_days=7) == []
    assert _row(db, entry_id)["notes"] == first["notes"]


def test_soft_deleted_rows_are_left_alone(db):
    entry_id = _timer(db, days_open=90)
    db.execute(text("UPDATE time_entries SET deleted_at = :n WHERE id = :id"),
               {"n": datetime.now(UTC), "id": entry_id})
    db.commit()
    assert repair.fetch_stale(db, min_age_days=7) == []
