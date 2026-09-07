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
from gdx_dispatch.routers import timeclock as timeclock_router

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


def test_day_clock_shows_on_job_detail_after_a_real_clock_in(session_factory):
    """#639: the job page's day clock must reflect the day clock the tech actually uses.

    The seed gives user-1 a Technician row ('tech-1'), which is the case that was
    broken: `POST /api/timeclock/clock-in` writes `technician_id = <user id>`
    (`_resolve_tech_id`), while the job page resolved a `Technician.id` first, so
    it read a key nothing writes and always said "Not clocked in". Both halves are
    asserted here so the test cannot pass by always answering the same way.
    """
    db = session_factory()
    try:
        # Another tech in the same shop is already on the clock. Single-tenant
        # means their row shares our tenant_id, so a reader that loses its
        # per-tech filter would show THEIR shift on OUR job page — this is the
        # assertion that fails if the filter is dropped rather than re-keyed.
        db.execute(
            text(
                "INSERT INTO timeclock_entries_router"
                " (id, tenant_id, technician_id, entry_type, clock_in_at,"
                "  created_at, updated_at)"
                " VALUES (:id, 'tenant-a', 'user-2', 'clock', :t, :t, :t)"
            ),
            {"id": uuid4().hex, "t": datetime.now(UTC) - timedelta(hours=3)},
        )
        db.commit()

        before = _as_json(
            mobile_router.get_mobile_job_detail(
                job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
            )
        )["clocks"]["day"]
        assert before["running"] is False, (
            "another tech's open shift must not render as this tech's day clock"
        )

        timeclock_router.post_clock_in(
            payload=timeclock_router.ClockActionRequest(),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )

        during = _as_json(
            mobile_router.get_mobile_job_detail(
                job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
            )
        )["clocks"]["day"]
        assert during["running"] is True, (
            "the day clock the tech started on /mobile/timeclock must show on the job page"
        )
        assert during["pays"] is True
        # Pin `since` to OUR row, not merely to something truthy: `since` is
        # str(clock_in_at) and is entailed by running, so comparing it to the
        # row this user just created is what makes it falsifiable.
        mine = db.execute(
            text(
                "SELECT clock_in_at FROM timeclock_entries_router"
                " WHERE tenant_id='tenant-a' AND technician_id='user-1'"
                "   AND clock_out_at IS NULL AND deleted_at IS NULL"
            )
        ).scalar_one()
        assert during["since"] == str(mine), (
            f"day clock shows {during['since']!r}, this tech's shift started {mine!r}"
        )

        timeclock_router.post_clock_out(
            payload=timeclock_router.ClockActionRequest(),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )

        after = _as_json(
            mobile_router.get_mobile_job_detail(
                job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
            )
        )["clocks"]["day"]
        assert after["running"] is False, "clocking out must clear it again"
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


def _day_clock(db) -> dict:
    return _as_json(
        mobile_router.get_mobile_job_detail(
            job_id=_JOB_ID, request=_request(), current_user=_TEST_USER, db=db
        )
    )["clocks"]["day"]


def test_day_clock_subtracts_ended_breaks_from_paid_time(session_factory):
    """#645: the card says "your paid time" — it may not pay out lunch.

    Reachable only since #647 made the day clock render at all; before that it
    always said "Not clocked in" and this was invisible.

    The assertion is the DIFFERENCE, not a fixed number: gross has to keep
    counting while the net figure drops by the break. A version that subtracted
    nothing leaves them equal, and a version that subtracted from the wrong
    clock leaves gross short — both fail here.
    """
    db = session_factory()
    try:
        timeclock_router.post_clock_in(
            payload=timeclock_router.ClockActionRequest(),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        # Back-date the shift so there is real elapsed time to subtract from.
        db.execute(
            text(
                "UPDATE timeclock_entries_router SET clock_in_at = :t"
                " WHERE tenant_id='tenant-a' AND technician_id='user-1'"
                "   AND clock_out_at IS NULL"
            ),
            {"t": (datetime.now(UTC) - timedelta(hours=4)).isoformat()},
        )
        db.commit()

        before = _day_clock(db)
        assert before["running"] is True
        assert before["on_break"] is False
        assert before["elapsed_minutes"] == before["gross_elapsed_minutes"], (
            "with no breaks taken, paid time and wall clock must agree"
        )

        entry_id = db.execute(
            text(
                "SELECT id FROM timeclock_entries_router"
                " WHERE tenant_id='tenant-a' AND technician_id='user-1'"
                "   AND clock_out_at IS NULL"
            )
        ).scalar_one()
        now_iso = datetime.now(UTC).isoformat()
        db.execute(
            text(
                "INSERT INTO timeclock_breaks_router"
                " (id, tenant_id, user_id, time_entry_id, type, started_at,"
                "  ended_at, duration_minutes, created_at)"
                " VALUES (:id, 'tenant-a', 'user-1', :eid, 'lunch', :s, :e, 30, :c)"
            ),
            {
                "id": uuid4().hex,
                "eid": entry_id,
                "s": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                "e": (datetime.now(UTC) - timedelta(hours=1, minutes=30)).isoformat(),
                "c": now_iso,
            },
        )
        db.commit()

        after = _day_clock(db)
        assert after["break_minutes"] == 30
        assert after["gross_elapsed_minutes"] >= before["gross_elapsed_minutes"], (
            "the wall clock does not stop for a break"
        )
        assert after["elapsed_minutes"] == after["gross_elapsed_minutes"] - 30, (
            "a 30-minute lunch must come off the clock that pays the tech; "
            f"got paid={after['elapsed_minutes']} gross={after['gross_elapsed_minutes']}"
        )
    finally:
        db.close()


def test_day_clock_says_on_break_rather_than_running(session_factory):
    """An OPEN break must not render as "Running" on the paying clock.

    We deliberately do not guess an open break's length — `break_minutes_by_entry`
    counts ended breaks only, and inventing a duration would fabricate hours.
    So the honest answer is the state, and that is what this pins.
    """
    db = session_factory()
    try:
        timeclock_router.post_clock_in(
            payload=timeclock_router.ClockActionRequest(),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        db.commit()
        assert _day_clock(db)["on_break"] is False

        started = datetime.now(UTC).isoformat()
        db.execute(
            text(
                "INSERT INTO timeclock_breaks_router"
                " (id, tenant_id, user_id, type, started_at, created_at)"
                " VALUES (:id, 'tenant-a', 'user-1', 'lunch', :s, :s)"
            ),
            {"id": uuid4().hex, "s": started},
        )
        db.commit()

        during = _day_clock(db)
        assert during["running"] is True, "the shift is still open during a break"
        assert during["on_break"] is True, (
            "a tech on lunch must not see a plain Running day clock"
        )
        assert during["on_break_since"] == started

        # The paid figure FREEZES at the break's start — a known stamp, so
        # nothing is guessed. Letting it tick on is #645's own defect (gross
        # under a "paid" label) inside the break window, and it would make the
        # number jump backwards when the break ended. Assert against the frozen
        # value rather than against gross, which keeps climbing.
        frozen = during["elapsed_minutes"]
        assert during["gross_elapsed_minutes"] >= frozen
        db.execute(
            text(
                "UPDATE timeclock_breaks_router SET started_at = :s"
                " WHERE tenant_id='tenant-a' AND user_id='user-1'"
                "   AND ended_at IS NULL"
            ),
            {"s": (datetime.now(UTC) - timedelta(minutes=90)).isoformat()},
        )
        db.execute(
            text(
                "UPDATE timeclock_entries_router SET clock_in_at = :t"
                " WHERE tenant_id='tenant-a' AND technician_id='user-1'"
                "   AND clock_out_at IS NULL"
            ),
            {"t": (datetime.now(UTC) - timedelta(hours=4)).isoformat()},
        )
        db.commit()
        later = _day_clock(db)
        # Clocked in 4h ago, break started 90m ago -> 150m paid, and it must
        # NOT be the ~240m of gross wall clock.
        assert later["elapsed_minutes"] == pytest.approx(150, abs=1), (
            f"paid time must stop at the break start; got {later['elapsed_minutes']}"
        )
        assert later["gross_elapsed_minutes"] == pytest.approx(240, abs=1)
    finally:
        db.close()


def test_another_techs_break_does_not_touch_this_clock(session_factory):
    """Single-tenant means user-2's break shares our tenant_id.

    Without the per-user filter their lunch would be deducted from OUR paid
    time. This is the assertion that fails if the break query is keyed on
    tenant alone.
    """
    db = session_factory()
    try:
        timeclock_router.post_clock_in(
            payload=timeclock_router.ClockActionRequest(),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        db.execute(
            text(
                "UPDATE timeclock_entries_router SET clock_in_at = :t"
                " WHERE tenant_id='tenant-a' AND technician_id='user-1'"
                "   AND clock_out_at IS NULL"
            ),
            {"t": (datetime.now(UTC) - timedelta(hours=4)).isoformat()},
        )
        now_iso = datetime.now(UTC).isoformat()
        db.execute(
            text(
                "INSERT INTO timeclock_breaks_router"
                " (id, tenant_id, user_id, type, started_at, ended_at,"
                "  duration_minutes, created_at)"
                " VALUES (:id, 'tenant-a', 'user-2', 'lunch', :s, :e, 45, :c)"
            ),
            {
                "id": uuid4().hex,
                "s": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                "e": (datetime.now(UTC) - timedelta(hours=1, minutes=15)).isoformat(),
                "c": now_iso,
            },
        )
        db.commit()

        day = _day_clock(db)
        assert day["break_minutes"] == 0, (
            "another tech's lunch must not come off this tech's paid time"
        )
        assert day["on_break"] is False
        assert day["elapsed_minutes"] == day["gross_elapsed_minutes"]
    finally:
        db.close()


def test_day_clock_flags_a_forgotten_shift(session_factory):
    """A shift nobody closed renders as "Running 16h" with no marker (#645).

    /api/timeclock/status already returns max_shift_hours + auto_clockout_at so
    its screen can flag one; the job card carried neither, so the tech most
    likely to be looking at it got no signal.
    """
    db = session_factory()
    try:
        timeclock_router.post_clock_in(
            payload=timeclock_router.ClockActionRequest(),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        db.commit()
        fresh = _day_clock(db)
        assert fresh["stale"] is False, "a shift that just started is not stale"
        assert fresh["auto_clockout_at"], "the card needs the auto-close time"

        # An ordinary long day must NOT trip it. This card has no snooze, so
        # firing at the 8h warning would light up on every normal working day
        # and the office would learn to ignore it.
        db.execute(
            text(
                "UPDATE timeclock_entries_router SET clock_in_at = :t"
                " WHERE tenant_id='tenant-a' AND technician_id='user-1'"
                "   AND clock_out_at IS NULL"
            ),
            {
                "t": (
                    datetime.now(UTC)
                    - timedelta(hours=timeclock_router.WARNING_AFTER_HOURS + 1)
                ).isoformat()
            },
        )
        db.commit()
        assert _day_clock(db)["stale"] is False, (
            "a 9-hour day is a long day, not a forgotten shift"
        )

        db.execute(
            text(
                "UPDATE timeclock_entries_router SET clock_in_at = :t"
                " WHERE tenant_id='tenant-a' AND technician_id='user-1'"
                "   AND clock_out_at IS NULL"
            ),
            {
                "t": (
                    datetime.now(UTC)
                    - timedelta(hours=timeclock_router.MAX_SHIFT_HOURS + 1)
                ).isoformat()
            },
        )
        db.commit()
        assert _day_clock(db)["stale"] is True, (
            "past the auto-close ceiling the card must say so"
        )
    finally:
        db.close()
def test_job_card_ignores_a_break_older_than_the_shift(session_factory):
    """The abandoned-break trap, on the job card (see the /status sibling).

    Nothing in this app closes a break, and prod carries one open since
    2026-04-08. An unbounded lookup would render "On break · 0m paid so far" on
    the card labelled "your paid time" for a tech who has worked all day.
    """
    db = session_factory()
    try:
        timeclock_router.post_clock_in(
            payload=timeclock_router.ClockActionRequest(),
            request=_request(),
            current_user=_TEST_USER,
            db=db,
        )
        db.execute(
            text(
                "UPDATE timeclock_entries_router SET clock_in_at = :t"
                " WHERE tenant_id='tenant-a' AND technician_id='user-1'"
                "   AND clock_out_at IS NULL"
            ),
            {"t": (datetime.now(UTC) - timedelta(hours=4)).isoformat()},
        )
        db.execute(
            text(
                "INSERT INTO timeclock_breaks_router"
                " (id, tenant_id, user_id, type, started_at, created_at)"
                " VALUES (:id, 'tenant-a', 'user-1', 'lunch', :s, :s)"
            ),
            {"id": uuid4().hex, "s": (datetime.now(UTC) - timedelta(days=150)).isoformat()},
        )
        db.commit()

        day = _day_clock(db)
        assert day["on_break"] is False, (
            "a break from before the shift is not this shift's break"
        )
        assert day["elapsed_minutes"] == pytest.approx(240, abs=1), (
            f"paid time must not collapse to zero; got {day['elapsed_minutes']}"
        )
    finally:
        db.close()
