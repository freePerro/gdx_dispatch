"""#645 sibling — /api/timeclock/status reports WORKED hours, not gross.

`TimeclockEntry.minutes` is gross elapsed: clock-out writes the whole span and
never subtracts breaks, which live in `timeclock_breaks_router`. The timesheet
and the payroll export already run every figure through
`core.timesheet_hours.break_minutes_by_entry`; this endpoint did not, so the
screen a tech reads their day off disagreed with the file the office pays from
by exactly one lunch.

The sibling of this bug on the mobile job card is pinned in
`test_mobile_job_clock.py`. Both surfaces now call the same helper — these
tests exist so they cannot drift apart again silently.

Behavioural, not source-text: every assertion here runs the real handler
against a real session. A `assert "break" in inspect.getsource(...)` would pass
for a comment.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.routers import timeclock as tc

_TENANT = "tenant-a"
_USER = {"user_id": "user-1", "role": "technician", "tenant_id": _TENANT}


def _request() -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": _TENANT}
    return req


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'status_breaks.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    engine.dispose()


def _open_shift(db, *, hours_ago: float, user_id: str = "user-1") -> str:
    entry_id = uuid4().hex
    started = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    db.execute(
        text(
            "INSERT INTO timeclock_entries_router"
            " (id, tenant_id, technician_id, entry_type, clock_in_at,"
            "  created_at, updated_at)"
            " VALUES (:id, :t, :u, 'clock', :s, :s, :s)"
        ),
        {"id": entry_id, "t": _TENANT, "u": user_id, "s": started},
    )
    db.commit()
    return entry_id


def _ended_break(db, entry_id: str | None, minutes: int, *, user_id: str = "user-1") -> None:
    now = datetime.now(UTC)
    db.execute(
        text(
            "INSERT INTO timeclock_breaks_router"
            " (id, tenant_id, user_id, time_entry_id, type, started_at,"
            "  ended_at, duration_minutes, created_at)"
            " VALUES (:id, :t, :u, :e, 'lunch', :s, :en, :m, :c)"
        ),
        {
            "id": uuid4().hex,
            "t": _TENANT,
            "u": user_id,
            "e": entry_id,
            "s": (now - timedelta(minutes=minutes + 10)).isoformat(),
            "en": (now - timedelta(minutes=10)).isoformat(),
            "m": minutes,
            "c": now.isoformat(),
        },
    )
    db.commit()


def _status(db):
    return tc.get_timeclock_status(
        request=_request(), technician_id=None, current_user=_USER, db=db
    )


def test_open_shift_elapsed_is_net_of_ended_breaks(session_factory):
    """The live figure on the timeclock screen must not pay out lunch."""
    db = session_factory()
    try:
        entry_id = _open_shift(db, hours_ago=4)
        before = _status(db)
        assert before.clocked_in is True
        assert before.open_shift_elapsed_hours == pytest.approx(4.0, abs=0.05)

        _ended_break(db, entry_id, 30)
        after = _status(db)
        # The DIFFERENCE is the assertion. A version that subtracts nothing
        # leaves this at ~4.0; a version that subtracts twice lands at ~3.0.
        assert after.open_shift_elapsed_hours == pytest.approx(3.5, abs=0.05), (
            f"30m lunch must come off the shift; got {after.open_shift_elapsed_hours}"
        )
    finally:
        db.close()


def test_today_hours_is_net_of_ended_breaks(session_factory):
    db = session_factory()
    try:
        entry_id = _open_shift(db, hours_ago=4)
        before = _status(db)
        _ended_break(db, entry_id, 45)
        after = _status(db)
        assert after.today_hours == pytest.approx(before.today_hours - 0.75, abs=0.05), (
            f"today_hours {after.today_hours} should be 45m below {before.today_hours}"
        )
    finally:
        db.close()


def test_another_techs_break_is_not_deducted(session_factory):
    """Single-tenant: user-2's rows share our tenant_id.

    Fails if the break lookup is keyed on tenant alone.
    """
    db = session_factory()
    try:
        _open_shift(db, hours_ago=4)
        before = _status(db)
        _open_shift(db, hours_ago=4, user_id="user-2")
        _ended_break(db, None, 45, user_id="user-2")
        after = _status(db)
        assert after.open_shift_elapsed_hours == pytest.approx(
            before.open_shift_elapsed_hours, abs=0.05
        ), "another tech's lunch must not shorten this tech's shift"
        assert after.on_break is False
    finally:
        db.close()


def test_open_break_freezes_the_worked_figure_at_its_start(session_factory):
    """An unfinished break has no defensible LENGTH — but a known START.

    `break_minutes_by_entry` counts ended breaks only, so the duration is never
    invented. The start stamp, though, is exact: worked-so-far stops there.
    Letting the figure tick on through the break is #645's own defect (gross
    under a "worked" label) in a narrower window, and it makes the number jump
    backwards when the break ends.
    """
    db = session_factory()
    try:
        _open_shift(db, hours_ago=4)
        assert _status(db).on_break is False

        db.execute(
            text(
                "INSERT INTO timeclock_breaks_router"
                " (id, tenant_id, user_id, type, started_at, created_at)"
                " VALUES (:id, :t, 'user-1', 'lunch', :s, :s)"
            ),
            {
                "id": uuid4().hex,
                "t": _TENANT,
                "s": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            },
        )
        db.commit()

        during = _status(db)
        assert during.on_break is True
        # Clocked in 4h ago, on break for the last 1h -> 3h worked, NOT 4h.
        assert during.open_shift_elapsed_hours == pytest.approx(3.0, abs=0.05), (
            "worked time must stop at the break start, not keep running; "
            f"got {during.open_shift_elapsed_hours}"
        )
        assert during.on_break_since is not None
        # today_hours must freeze with it. The timeclock screen shows BOTH, so
        # freezing only one leaves two numbers on one card disagreeing by the
        # length of the running break (caught on the throwaway, 2026-09-07).
        assert during.today_hours == pytest.approx(
            during.open_shift_elapsed_hours, abs=0.05
        ), (
            f"today={during.today_hours} and open={during.open_shift_elapsed_hours} "
            "must not drift apart during an open break"
        )
    finally:
        db.close()


def test_overnight_shifts_breaks_are_still_subtracted(session_factory):
    """A shift clocked in yesterday contributes its post-midnight portion to
    today_hours, so its breaks have to be in the break set too.

    The date filter alone would leave exactly that shift's lunch unsubtracted —
    the one case where the aggregate and the timesheet would disagree.
    """
    db = session_factory()
    try:
        entry_id = _open_shift(db, hours_ago=30)
        before = _status(db)
        _ended_break(db, entry_id, 60)
        after = _status(db)
        assert after.today_hours == pytest.approx(before.today_hours - 1.0, abs=0.05), (
            f"overnight shift's break unsubtracted: {before.today_hours} -> {after.today_hours}"
        )
    finally:
        db.close()


def test_status_and_clock_in_agree_on_what_counts_as_open(session_factory):
    """One rule for "is this tech clocked in", shared with the writers.

    An earlier revision filtered this endpoint's open-shift lookup to
    `entry_type='clock'` as a defensive measure. `POST /clock-in`'s duplicate
    guard has no such filter, and `PATCH /api/timeclock/entries/{id}` with
    `clock_out_at=null` survives `exclude_unset` and reopens a 'manual' row —
    so the filter made /status answer "not clocked in" for a row that makes
    clock-in reply 400 "already clocked in". A dead end on the endpoint that
    decides whether someone is being paid.

    Prod carries 60 rows, all `entry_type='clock'` (measured 2026-09-07), so
    the filter bought nothing and cost consistency.
    """
    db = session_factory()
    try:
        now = datetime.now(UTC).isoformat()
        db.execute(
            text(
                "INSERT INTO timeclock_entries_router"
                " (id, tenant_id, technician_id, entry_type, clock_in_at,"
                "  created_at, updated_at)"
                " VALUES (:id, :t, 'user-1', 'manual', :s, :s, :s)"
            ),
            {"id": uuid4().hex, "t": _TENANT, "s": now},
        )
        db.commit()

        assert _status(db).clocked_in is True, (
            "an open row is an open row — /status must not disagree with the "
            "guard that refuses a second clock-in for it"
        )
        with pytest.raises(Exception) as exc:
            tc.post_clock_in(
                payload=tc.ClockActionRequest(),
                request=_request(),
                current_user=_USER,
                db=db,
            )
        assert "409" in str(exc.value) or "400" in str(exc.value) or "already" in str(
            exc.value
        ).lower(), f"clock-in should refuse; got {exc.value}"
    finally:
        db.close()


def test_a_break_older_than_the_shift_is_not_this_shifts_break(session_factory):
    """The abandoned-break trap. This is the one that would have hit prod.

    NOTHING in this app closes a break: `post_clock_out` does not touch them,
    the clock-in auto-close closes only the shift, and there is no sweep.
    Production carries an open `lunch` row started 2026-04-08 (measured
    2026-09-07) that will never end.

    An unbounded "does this user have an open break" lookup therefore answers
    YES forever. Worked time would freeze at that ancient stamp and clamp to
    zero, so the moment that tech clocked in, two surfaces would report ZERO
    paid time for a full day's work — #645 pointed the other way, and
    under-reporting someone's pay is the worse direction.
    """
    db = session_factory()
    try:
        _open_shift(db, hours_ago=4)
        db.execute(
            text(
                "INSERT INTO timeclock_breaks_router"
                " (id, tenant_id, user_id, type, started_at, created_at)"
                " VALUES (:id, :t, 'user-1', 'lunch', :s, :s)"
            ),
            {
                "id": uuid4().hex,
                "t": _TENANT,
                # Five months before the shift — the prod row's shape.
                "s": (datetime.now(UTC) - timedelta(days=150)).isoformat(),
            },
        )
        db.commit()

        st = _status(db)
        assert st.clocked_in is True
        assert st.on_break is False, (
            "a break that started before this shift is not this shift's break"
        )
        assert st.open_shift_elapsed_hours == pytest.approx(4.0, abs=0.05), (
            f"worked time must not freeze on an abandoned break; got "
            f"{st.open_shift_elapsed_hours}"
        )
        assert st.today_hours and st.today_hours > 0, (
            "a working tech must never be reported at zero paid hours"
        )
    finally:
        db.close()


def test_a_break_started_during_the_shift_still_counts(session_factory):
    """The other half of the bound — it must not throw away real breaks.

    Without this, "ignore breaks older than the shift" could be implemented as
    "ignore all breaks" and the test above would still pass.
    """
    db = session_factory()
    try:
        _open_shift(db, hours_ago=4)
        db.execute(
            text(
                "INSERT INTO timeclock_breaks_router"
                " (id, tenant_id, user_id, type, started_at, created_at)"
                " VALUES (:id, :t, 'user-1', 'lunch', :s, :s)"
            ),
            {
                "id": uuid4().hex,
                "t": _TENANT,
                "s": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            },
        )
        db.commit()

        st = _status(db)
        assert st.on_break is True
        assert st.open_shift_elapsed_hours == pytest.approx(3.0, abs=0.05)
    finally:
        db.close()


def test_overnight_shift_does_not_pay_yesterdays_lunch_twice(session_factory):
    """today_hours counts only breaks that STARTED today.

    An open overnight shift contributes just its post-midnight portion to
    today_hours, so netting the whole shift's breaks would take a 19:00 lunch
    off today AND off yesterday's own timesheet — the same 30 minutes twice, in
    opposite days.
    """
    db = session_factory()
    try:
        entry_id = _open_shift(db, hours_ago=14)
        before = _status(db)
        # A break in the part of the shift that fell YESTERDAY.
        yesterday_break = datetime.now(UTC) - timedelta(hours=13)
        db.execute(
            text(
                "INSERT INTO timeclock_breaks_router"
                " (id, tenant_id, user_id, time_entry_id, type, started_at,"
                "  ended_at, duration_minutes, created_at)"
                " VALUES (:id, :t, 'user-1', :e, 'lunch', :s, :en, 30, :s)"
            ),
            {
                "id": uuid4().hex,
                "t": _TENANT,
                "e": entry_id,
                "s": yesterday_break.isoformat(),
                "en": (yesterday_break + timedelta(minutes=30)).isoformat(),
            },
        )
        db.commit()
        after = _status(db)

        same_day = yesterday_break.date() == datetime.now(UTC).date()
        if same_day:
            # The clock happened to run inside one UTC day — then it DOES count.
            assert after.today_hours == pytest.approx(before.today_hours - 0.5, abs=0.05)
        else:
            assert after.today_hours == pytest.approx(before.today_hours, abs=0.05), (
                "a break taken yesterday must not come off today's hours"
            )
    finally:
        db.close()


def test_status_and_break_start_agree_on_what_counts_as_on_break(session_factory):
    """The reader/writer twin of the entry_type test above.

    `open_break_in_shift` bounds the READ to the current shift. If
    `POST /break/start`'s duplicate guard is not bounded the same way, prod's
    never-ended 2026-04-08 `lunch` puts that technician in a trap forever:
    the readers say "not on break" so the UI shows **Start Break** and hides
    **End Break**, and Start Break 409s on the stale row. The tech can neither
    start nor end a break, so their lunches never get recorded and the office
    OVERPAYS every one of them.

    This is the assertion that fails if the two ever drift apart again.
    """
    db = session_factory()
    try:
        _open_shift(db, hours_ago=2)
        db.execute(
            text(
                "INSERT INTO timeclock_breaks_router"
                " (id, tenant_id, user_id, type, started_at, created_at)"
                " VALUES (:id, :t, 'user-1', 'lunch', :s, :s)"
            ),
            {
                "id": uuid4().hex,
                "t": _TENANT,
                "s": (datetime.now(UTC) - timedelta(days=150)).isoformat(),
            },
        )
        db.commit()

        assert _status(db).on_break is False, "the stale break is not this shift's"

        # ...and the writer must agree: starting a break has to WORK.
        import asyncio

        brk = asyncio.run(
            tc.start_break(
                payload=tc.BreakStartRequest(type="lunch"),
                request=_request(),
                current_user=_USER,
                db=db,
            )
        )
        assert brk.id, "a stale break must not block starting a real one"
        assert _status(db).on_break is True, "the new break is this shift's"
    finally:
        db.close()


def test_a_real_in_shift_break_still_blocks_a_second_one(session_factory):
    """The other half — the guard must not become a no-op.

    Without this, "ignore stale breaks" could be implemented as "ignore every
    break" and the test above would still pass.
    """
    import asyncio

    db = session_factory()
    try:
        _open_shift(db, hours_ago=2)
        asyncio.run(
            tc.start_break(
                payload=tc.BreakStartRequest(type="lunch"),
                request=_request(),
                current_user=_USER,
                db=db,
            )
        )
        with pytest.raises(Exception) as exc:
            asyncio.run(
                tc.start_break(
                    payload=tc.BreakStartRequest(type="rest"),
                    request=_request(),
                    current_user=_USER,
                    db=db,
                )
            )
        assert "409" in str(exc.value) or "already" in str(exc.value).lower(), exc.value
    finally:
        db.close()


def test_break_start_still_refuses_when_not_clocked_in(session_factory):
    """The guard must not become a no-op off the clock.

    Bounding the duplicate check to the current shift left `active = None`
    whenever there was no open shift, so every retry, double-tap or offline
    replay minted another `ended_at IS NULL` row that nothing can close —
    manufacturing the very row class this change exists to stop accumulating.
    """
    import asyncio

    db = session_factory()
    try:
        # No shift at all, and one open break already on file.
        db.execute(
            text(
                "INSERT INTO timeclock_breaks_router"
                " (id, tenant_id, user_id, type, started_at, created_at)"
                " VALUES (:id, :t, 'user-1', 'lunch', :s, :s)"
            ),
            {"id": uuid4().hex, "t": _TENANT, "s": datetime.now(UTC).isoformat()},
        )
        db.commit()

        with pytest.raises(Exception) as exc:
            asyncio.run(
                tc.start_break(
                    payload=tc.BreakStartRequest(type="rest"),
                    request=_request(),
                    current_user=_USER,
                    db=db,
                )
            )
        assert "409" in str(exc.value) or "already" in str(exc.value).lower(), exc.value

        open_rows = db.execute(
            text(
                "SELECT count(*) FROM timeclock_breaks_router"
                " WHERE tenant_id=:t AND user_id='user-1' AND ended_at IS NULL"
            ),
            {"t": _TENANT},
        ).scalar_one()
        assert open_rows == 1, f"a refused start must mint nothing; found {open_rows}"
    finally:
        db.close()
