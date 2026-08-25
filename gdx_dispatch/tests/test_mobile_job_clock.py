"""The per-job clock on mobile: what it records, and what it refuses to pay.

The endpoints exercised here (`/api/mobile/jobs/{id}/clock-in|clock-out`) sat
with zero frontend callers from the initial public release until 2026-08-25 —
the orphan-endpoint class CLAUDE.md forbids. Wiring them up made their money
semantics load-bearing for the first time, and the shape they shipped with was
the one #154 killed everywhere else: close with unclamped wall-clock elapsed.

`time_entries.duration_minutes` IS payroll hours. payroll.py:248 sums
COALESCE(duration_minutes, 0) with no rate filter and no entry_type filter, so
anything stored there reaches hours_worked, overtime and gross pay. These tests
pin the rule that follows from that: a tech tapping Stop records the span for
the office and banks ZERO payable minutes. Only closeout-attested hours pay.

Every assertion here fails if the code goes back to writing elapsed.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.routers import gps as _gps  # noqa: F401  (registers TechnicianLocation)
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers import payroll as payroll_router

_TEST_USER = {"user_id": "user-1", "role": "technician", "tenant_id": "tenant-a"}

_JOB_ID = uuid4().hex
_CUST_ID = uuid4().hex


def _as_json(response) -> dict:
    return json.loads(response.body)


def _request(tenant_id: str = "tenant-a") -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": tenant_id}
    return req


def _seed(db: Session) -> None:
    now = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
    db.execute(
        text(
            "INSERT INTO customers (id, name, phone, email, address, company_id)"
            " VALUES (:id, 'Acme Customer', '555-1111', 'a@example.com', '123 Main', 'tenant-a')"
        ),
        {"id": _CUST_ID},
    )
    db.execute(
        text(
            "INSERT INTO technicians (id, company_id, user_id, active, created_at)"
            # `1` works on SQLite and is a DatatypeMismatch on Postgres, where
            # `active` is a real boolean. Bind it instead of inlining a literal.
            " VALUES ('tech-1', 'tenant-a', 'user-1', :active, :created_at)"
        ),
        {"active": True, "created_at": now},
    )
    db.execute(
        text(
            """
            INSERT INTO jobs (
                id, company_id, customer_id, title, description, dispatch_status,
                assigned_to, scheduled_at, created_at, deleted_at
            ) VALUES (
                :id, 'tenant-a', :customer_id, 'Garage Door Repair', 'Broken spring',
                'assigned', 'tech-1', :scheduled_at, :created_at, NULL
            )
            """
        ),
        {"id": _JOB_ID, "customer_id": _CUST_ID, "scheduled_at": now, "created_at": now},
    )
    db.commit()


@pytest.fixture()
def session_factory(tmp_path):
    # Runs on SQLite by default and on Postgres when GDX_PROOF_PG_URL points at
    # one. The money assertions below are about SQL this code emits by hand
    # (a CASE on notes, a literal `duration_minutes = 0`), and "it passes on
    # SQLite" is not evidence it runs on the plane prod is on — the plan for
    # this feature said as much before a line of it was written.
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
            f"sqlite:///{tmp_path / 'job_clock.sqlite3'}",
            connect_args={"check_same_thread": False},
        )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    _seed(db)
    db.close()
    yield SessionLocal
    engine.dispose()


def _open_entry(db: Session) -> dict | None:
    row = db.execute(
        text(
            "SELECT id, clock_in, clock_out, duration_minutes, hourly_rate, notes"
            " FROM time_entries WHERE job_id = :jid ORDER BY clock_in DESC LIMIT 1"
        ),
        {"jid": _JOB_ID},
    ).mappings().first()
    return dict(row) if row else None


def _backdate_open_entry(db: Session, minutes: int) -> None:
    """Make the open timer look `minutes` old, so elapsed is unmistakably > 0."""
    db.execute(
        text("UPDATE time_entries SET clock_in = :ci WHERE job_id = :jid AND clock_out IS NULL"),
        {"ci": datetime.now(UTC) - timedelta(minutes=minutes), "jid": _JOB_ID},
    )
    db.commit()


# ── the money rule ────────────────────────────────────────────────────────


def test_manual_stop_banks_zero_minutes_not_elapsed(session_factory):
    """A 187-minute span must store 0, not 187.

    This is the counterfactual: restore the old
    `duration_minutes = int(round(delta_seconds / 60))` and this fails on the
    first assertion with 187 != 0.
    """
    db = session_factory()
    try:
        mobile_router.mobile_clock_in(
            job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
        )
        _backdate_open_entry(db, 187)

        r = mobile_router.mobile_clock_out(
            job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
        )
        assert r.status_code == 200
        body = _as_json(r)

        row = _open_entry(db)
        assert row is not None
        assert row["clock_out"] is not None, "the timer must actually end"
        assert row["duration_minutes"] == 0, (
            f"stored {row['duration_minutes']} payable minutes; elapsed is not evidence (#154)"
        )
        assert row["hourly_rate"] is None, "a stopped timer is not priced here"

        # The span is recorded where a human reads it, not banked.
        assert "187" in (row["notes"] or ""), f"elapsed span lost from notes: {row['notes']!r}"
        assert mobile_router.MOBILE_STOP_LABOR_NOTE in (row["notes"] or "")

        # The response must not hand the tech a number that reads like earnings.
        assert body["elapsed_minutes"] == 187
        assert body["recorded_minutes"] == 0
        assert body["payable"] is False
        assert body["duration_minutes"] == 0
    finally:
        db.close()


def test_stopped_timer_adds_no_payroll_hours(session_factory):
    """The guard that matters: payroll must see nothing from a stopped timer.

    Asserting through payroll's own reader, not through the column, because
    payroll is the surface that pays — `_fetch_tech_hours` ignores hourly_rate
    entirely, so a NULL rate is no protection.
    """
    db = session_factory()
    try:
        mobile_router.mobile_clock_in(
            job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
        )
        _backdate_open_entry(db, 240)
        mobile_router.mobile_clock_out(
            job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
        )

        today = datetime.now(UTC).date()
        hours = payroll_router._fetch_tech_hours(
            db, tenant_id="tenant-a", start=today - timedelta(days=1), end=today + timedelta(days=1)
        )
        total = sum(sum(days.values()) for days in hours.values())
        assert total == 0.0, f"a stopped timer paid out {total}h of unattested elapsed time"
    finally:
        db.close()


def test_arrival_then_stop_still_banks_zero(session_factory):
    """Arrival auto-starts the timer; stopping THAT one is the real field path.

    The tech never taps clock-in — `/arrived` opens the row. If only the
    manual-clock-in path were guarded, this is where elapsed would leak.
    """
    db = session_factory()
    try:
        mobile_router.mobile_job_arrived(
            job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
        )
        _backdate_open_entry(db, 95)
        mobile_router.mobile_clock_out(
            job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
        )
        row = _open_entry(db)
        assert row["duration_minutes"] == 0
        assert "95" in (row["notes"] or "")
    finally:
        db.close()


# ── the toggle contract ───────────────────────────────────────────────────


def test_job_detail_exposes_both_clocks(session_factory):
    """The UI cannot render a state-reflecting toggle without this payload."""
    db = session_factory()
    try:
        r = mobile_router.get_mobile_job_detail(
            job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
        )
        assert r.status_code == 200
        clocks = _as_json(r)["clocks"]
        assert clocks["job"]["running"] is False
        assert clocks["job"]["pays"] is False, "the job clock must never claim to pay"
        assert clocks["day"]["pays"] is True

        mobile_router.mobile_job_arrived(
            job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
        )
        _backdate_open_entry(db, 42)

        r2 = mobile_router.get_mobile_job_detail(
            job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
        )
        job_clock = _as_json(r2)["clocks"]["job"]
        assert job_clock["running"] is True
        assert job_clock["entry_id"]
        assert job_clock["elapsed_minutes"] == 42
    finally:
        db.close()


def test_clock_in_on_running_timer_conflicts(session_factory):
    """Why the control must be a toggle, not a Start button.

    Arrival already opened a timer, so a second start is a 409. Pinning this
    keeps the frontend honest: it has to read state before offering an action.
    """
    db = session_factory()
    try:
        mobile_router.mobile_job_arrived(
            job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
        )
        r = mobile_router.mobile_clock_in(
            job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
        )
        assert r.status_code == 409
        assert "Already clocked in" in _as_json(r)["detail"]
    finally:
        db.close()


def test_clock_out_with_no_open_timer_is_404(session_factory):
    db = session_factory()
    try:
        r = mobile_router.mobile_clock_out(
            job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
        )
        assert r.status_code == 404
    finally:
        db.close()


def test_soft_deleted_timer_is_not_open(session_factory):
    """Alignment with jobs.py::_open_job_timers, which filters deleted_at.

    Without the guard the toggle would offer Stop on a row the closeout closer
    can never see — two writers disagreeing about the same timer.
    """
    db = session_factory()
    try:
        mobile_router.mobile_clock_in(
            job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
        )
        db.execute(
            text("UPDATE time_entries SET deleted_at = :now WHERE job_id = :jid"),
            {"now": datetime.now(UTC), "jid": _JOB_ID},
        )
        db.commit()

        found = mobile_router._find_open_time_entry(
            db, "tenant-a", "user-1", job_id=_JOB_ID, entry_type="job"
        )
        assert found is None
    finally:
        db.close()
