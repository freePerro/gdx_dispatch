"""Closeout labor trail (2026-07-17) — the per-job timer and who can read it.

Mobile's arrival auto-clocks-in a per-job timer (`mobile_job_arrived` writes
TimeEntry(entry_type='job', user_id=user, tech_id=Technician.id, clock_out
NULL)). Closeout was the only thing that could end it and never did: it looked
for `tech_id == user_id`, which mobile never writes, so it missed the timer and
added a SECOND synthetic row. Prod carried 5 permanently-open timers (oldest
2026-04-29) alongside 4 synthetic rows.

Both rows were then invisible to the readers that matter:
  * payroll.py:246-263 groups by `user_id` and skips rows with a NULL
    clock_out — the synthetic left user_id NULL, the timer left clock_out NULL,
    so payroll saw ZERO hours for every job.
  * labor.py:110 / job_costing.py:200 cost from the STORED `hourly_rate`
    column and never re-resolve it — neither writer set it, so all labor cost
    the $50 default regardless of the tech.

The governing rule, learned the hard way over three adversarial rounds:
CLOSEOUT MAY NOT INVENT HOURS. Attested time is evidence; wall-clock elapsed
is not — it measures how long a tech forgot to close a timer. Every attempt to
salvage elapsed (raw, then clamped to 12h) produced a worse bug than the leak
it fixed, because an overpayment gets cashed while a missing hour gets
reported.

Pinned here, against those readers rather than against the SQL shape:
1. Closeout CLOSES the arrival timer instead of leaving it open.
2. It does not write a second row when a timer is open (no double labor).
3. A closed timer is payroll-visible (user_id + clock_out set).
4. Every row it writes is costed at the tech's rate (hourly_rate snapshotted).
5. Re-closeout RESTATES the job's row rather than adding another — including
   when a different human (a dispatcher) does the re-closeout, and when a
   re-arrival has opened a fresh timer in between.
6. Every tech's timer on a multi-tech job closes, under THAT tech's identity
   — but unpaid (0), never guessed from elapsed.
7. Attested hours anchor to the real clock_in, so hours land in the day the
   work happened, not the day someone got around to closing out.
8. A stale timer pays nothing. Elapsed would book ~1,900h ≈ $180k at
   job_costing's $95/h default (job_costing.py:201) and the same into gross pay.
9. A closer who is not the tech does NOT get the hours in their paycheck —
   the unattributed synthetic keeps its pre-existing shape (user_id NULL).
10. [2026-08-25] A tech who STOPS the job timer by hand and then closes out is
   paid the attested hours and nothing more. This case is why the mobile Stop
   button stayed unbuilt for a month: stopping used to bank wall-clock elapsed,
   so an attested 2h job on a 5h-old timer paid 7h across two rows. The mobile
   path now banks zero on a manual stop (mobile.py::_close_open_time_entry), so
   the second row closeout writes is the only payable one.

Payroll assertions run payroll's OWN query (`_fetch_tech_hours`), not a
paraphrase of it — an earlier version of this file hand-rolled the filter and
was structurally blind to the bugs above.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy import text as _sa_text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    JobCloseout,
    JobPartNeeded,
    Payment,
    Technician,
    TimeEntry,
)
from gdx_dispatch.modules.inventory.models import JobPart, Part
from gdx_dispatch.routers.jobs import (
    CLOSEOUT_LABOR_NOTE,
    CloseoutPayload,
    closeout_job,
)

TENANT = "tenant-1"
USER = "user-michael"
RATE = 42.5


@pytest.fixture
def db():
    # Postgres when GDX_PROOF_PG_URL points at one, SQLite otherwise. The
    # closeout target chain is money code and its newest predicate is a
    # `notes LIKE` — "green on SQLite" is not evidence it behaves on the plane
    # prod runs. (The 2026-08-25 PG run of the sibling suites caught two bugs
    # SQLite hid: an int-for-boolean literal and an unenforced FK.)
    pg_url = os.environ.get("GDX_PROOF_PG_URL")
    if pg_url:
        engine = create_engine(pg_url)
        with engine.begin() as conn:
            # Not drop_all: estimates <-> proposal_tiers is a circular FK that
            # SQLAlchemy cannot sort for DROP.
            conn.execute(_sa_text("DROP SCHEMA public CASCADE"))
            conn.execute(_sa_text("CREATE SCHEMA public"))
    else:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    # create_all FIRST: it sorts tables topologically, and Postgres enforces
    # the FK order that SQLite ignores (jobs references customers, so the
    # hand-ordered list below fails on PG with "relation customers does not
    # exist"). The explicit list stays for the tables outside TenantBase's
    # metadata (Part, JobPart) and is a checkfirst no-op for the rest.
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    for tbl in [
        Job.__table__,
        Customer.__table__,
        Invoice.__table__,
        InvoiceLine.__table__,
        Payment.__table__,
        JobPartNeeded.__table__,
        JobCloseout.__table__,
        Part.__table__,
        JobPart.__table__,
        Technician.__table__,
        TimeEntry.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _request() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    req.state.tenant_id = TENANT
    return req


def _seed_job(db) -> Job:
    # A real Customer row, not a bare uuid: Postgres enforces the
    # jobs.customer_id FK that SQLite silently ignores, so the original
    # fixture's orphan customer_id made this whole file SQLite-only.
    customer = Customer(id=uuid4(), name="Acme Customer", company_id=TENANT)
    db.add(customer)
    db.flush()
    job = Job(
        customer_id=customer.id,
        title="Door repair",
        description="t",
        lifecycle_stage="in_progress",
        dispatch_status="on_site",
        billing_status="unbilled",
        company_id=TENANT,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _seed_technician_for(db, *, user_id: str, rate: float | None = RATE) -> Technician:
    tech = Technician(
        id=str(uuid4()),
        company_id=TENANT,
        user_id=user_id,
        name=f"Tech {user_id}",
        active=True,
        hourly_rate=None if rate is None else Decimal(str(rate)),
        created_at=datetime.now(UTC),
    )
    db.add(tech)
    db.commit()
    db.refresh(tech)
    return tech


def _seed_technician(db, *, rate: float | None = RATE) -> Technician:
    return _seed_technician_for(db, user_id=USER, rate=rate)


def _seed_arrival_timer_for(
    db, job, tech, *, user_id: str, arrived_ago_hours: float = 3.0
) -> TimeEntry:
    """Exactly what mobile_job_arrived writes: open, user_id set, tech_id =
    Technician.id, no hourly_rate."""
    entry = TimeEntry(
        id=uuid4(),
        company_id=TENANT,
        job_id=job.id,
        tech_id=tech.id,
        user_id=user_id,
        clock_in=datetime.now(UTC) - timedelta(hours=arrived_ago_hours),
        clock_out=None,
        duration_minutes=None,
        entry_type="job",
        created_at=datetime.now(UTC),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def _seed_arrival_timer(db, job, tech, *, arrived_ago_hours: float = 3.0) -> TimeEntry:
    return _seed_arrival_timer_for(
        db, job, tech, user_id=USER, arrived_ago_hours=arrived_ago_hours
    )


def _closeout(db, job, *, hours: float = 2.0):
    return closeout_job(
        payload=CloseoutPayload(parts=[], hours=hours, no_parts_used=True),
        job_id=str(job.id),
        request=_request(),
        current_user={"user_id": USER, "tenant_id": TENANT, "role": "technician"},
        db=db,
    )


def _entries(db, job) -> list[TimeEntry]:
    return db.execute(
        select(TimeEntry).where(TimeEntry.job_id == job.id).order_by(TimeEntry.clock_in)
    ).scalars().all()


def _payroll_hours(db) -> dict[str, float]:
    """What payroll ACTUALLY pays out, via payroll's own query rather than a
    paraphrase of it: {user_id: total hours} over a window wide enough to
    catch anything closeout wrote."""
    from gdx_dispatch.routers.payroll import _fetch_tech_hours

    by_tech = _fetch_tech_hours(
        db,
        tenant_id=TENANT,
        start=(datetime.now(UTC) - timedelta(days=365)).date(),
        end=(datetime.now(UTC) + timedelta(days=365)).date(),
    )
    # A closed 0-hour row still groups, so drop the unpaid ones: what matters
    # is hours payable, not whether the tech has a row.
    totals = {tech: sum(days.values()) for tech, days in by_tech.items()}
    return {tech: hours for tech, hours in totals.items() if hours}


def test_closeout_closes_the_arrival_timer(db):
    job, tech = _seed_job(db), _seed_technician(db)
    timer = _seed_arrival_timer(db, job, tech)
    assert timer.clock_out is None

    _closeout(db, job, hours=2.0)
    db.refresh(timer)

    assert timer.clock_out is not None, "arrival timer left running after closeout"
    assert timer.duration_minutes == 120


def test_closeout_does_not_double_count_when_timer_open(db):
    """The regression: closeout missed the timer and added a second row."""
    job, tech = _seed_job(db), _seed_technician(db)
    _seed_arrival_timer(db, job, tech)

    _closeout(db, job, hours=2.0)

    entries = _entries(db, job)
    assert len(entries) == 1, f"expected the timer reused, got {len(entries)} rows"
    assert sum(e.duration_minutes or 0 for e in entries) == 120


def test_closed_timer_is_visible_to_payroll(db):
    """payroll groups by user_id and skips NULL clock_out — a row missing
    either is invisible, which is what prod had (zero job hours payable)."""
    job, tech = _seed_job(db), _seed_technician(db)
    _seed_arrival_timer(db, job, tech)

    assert _payroll_hours(db) == {}, "open timer should not be payable yet"

    _closeout(db, job, hours=2.0)

    assert _payroll_hours(db) == {USER: pytest.approx(2.0)}


def test_labor_row_is_costed_at_the_techs_rate(db):
    """Cost readers use the stored hourly_rate and never re-resolve it, so an
    unset rate silently costs the $50 default forever."""
    job, tech = _seed_job(db), _seed_technician(db, rate=RATE)
    _seed_arrival_timer(db, job, tech)

    _closeout(db, job, hours=2.0)

    entry = _entries(db, job)[0]
    assert entry.hourly_rate is not None, "rate unset -> costed at the default"
    assert float(entry.hourly_rate) == RATE


def test_synthetic_entry_when_no_timer_open(db):
    """A tech who never tapped 'I'm here' still leaves a costed labor trail."""
    job = _seed_job(db)
    _seed_technician(db)

    _closeout(db, job, hours=1.5)

    entries = _entries(db, job)
    assert len(entries) == 1
    assert entries[0].duration_minutes == 90
    assert float(entries[0].hourly_rate) == RATE, "uncosted -> $95 default"


def test_closer_is_never_paid_for_someone_elses_hours(db):
    """An unattested synthetic keeps its pre-existing unattributed shape. The
    closer may be a dispatcher; stamping their user_id would move a tech's
    hours into the dispatcher's paycheck — worse than the hours being absent,
    which is the status quo this must not regress."""
    job = _seed_job(db)
    _seed_technician(db)

    _closeout(db, job, hours=1.5)

    assert _payroll_hours(db) == {}, "closer must not be paid for the synthetic"


def test_re_closeout_updates_its_row_rather_than_adding_one(db):
    """/closeout has no unique constraint or replay guard; a double submit
    must not double the job's labor."""
    job, tech = _seed_job(db), _seed_technician(db)
    _seed_arrival_timer(db, job, tech)

    _closeout(db, job, hours=2.0)
    _closeout(db, job, hours=3.0)

    entries = _entries(db, job)
    assert len(entries) == 1, f"re-closeout duplicated labor: {len(entries)} rows"
    assert entries[0].duration_minutes == 180, "re-closeout should restate hours"


def test_attested_hours_anchor_to_clock_in_not_closeout_time(db):
    """Payroll windows on DATE(clock_in). A timer started on arrival must keep
    its hours in the day the work happened, even if closeout comes later."""
    job, tech = _seed_job(db), _seed_technician(db)
    timer = _seed_arrival_timer(db, job, tech, arrived_ago_hours=30.0)
    started = timer.clock_in

    _closeout(db, job, hours=2.0)
    db.refresh(timer)

    assert timer.clock_in == started, "clock_in moved; hours changed pay period"
    assert timer.clock_out == started + timedelta(hours=2)
    assert timer.duration_minutes == 120


def test_no_hours_attested_closes_timer_but_pays_nothing(db):
    """Closing with hours=0 still stops the leak — it just doesn't invent
    hours from the clock. The office sees a closed 0-hour row and can correct
    it via /api/labor, which is the honest failure mode."""
    job, tech = _seed_job(db), _seed_technician(db)
    _seed_arrival_timer(db, job, tech, arrived_ago_hours=1.0)

    _closeout(db, job, hours=0)

    entries = _entries(db, job)
    assert len(entries) == 1
    assert entries[0].clock_out is not None, "timer left open when hours=0"
    assert entries[0].duration_minutes == 0
    assert _payroll_hours(db) == {}


def test_stale_timer_with_no_attested_hours_pays_nothing(db):
    """The catastrophic case. Prod's oldest timer has been open since
    2026-04-29; hours=0 is allowed (the require_hours gate is off by default).
    Billing measured elapsed books ~1,900h — about $180k on one job at
    job_costing's $95/h default — and the same into gross pay. Elapsed
    measures forgetting, not work: it is worth zero, not a smaller guess."""
    job, tech = _seed_job(db), _seed_technician(db)
    timer = _seed_arrival_timer(db, job, tech, arrived_ago_hours=79 * 24)

    _closeout(db, job, hours=0)
    db.refresh(timer)

    assert timer.clock_out is not None, "timer left running"
    assert timer.duration_minutes == 0, "invented hours from a forgotten timer"
    assert _payroll_hours(db) == {}, "fabricated hours reached payroll"


def test_colleague_timer_closes_unpaid_not_guessed(db):
    """A colleague's timer must stop leaking WITHOUT being guessed at. The
    closer attested for the job, not for someone else's clock — turning an
    unpaid 0 into a fabricated 12h is strictly worse, because an overpayment
    gets cashed where a missing hour gets reported."""
    job = _seed_job(db)
    mine = _seed_technician(db)
    other = _seed_technician_for(db, user_id="user-other", rate=60.0)
    _seed_arrival_timer(db, job, mine)
    theirs = _seed_arrival_timer_for(
        db, job, other, user_id="user-other", arrived_ago_hours=20.0
    )

    _closeout(db, job, hours=2.0)
    db.refresh(theirs)

    assert theirs.clock_out is not None, "colleague's timer left running"
    assert theirs.user_id == "user-other", "colleague's hours reattributed"
    assert theirs.duration_minutes == 0, "colleague's elapsed was invented"

    paid = _payroll_hours(db)
    assert paid == {USER: pytest.approx(2.0)}, "only attested hours are payable"


def test_dispatcher_re_closeout_restates_rather_than_stacks(db):
    """Tech closes 2h, then a dispatcher re-closes the same job at 3h. Keying
    the owned row on the CLOSER would leave two rows and bill 5h for a 3h job."""
    job, tech = _seed_job(db), _seed_technician(db)
    _seed_arrival_timer(db, job, tech)

    _closeout(db, job, hours=2.0)
    closeout_job(
        payload=CloseoutPayload(parts=[], hours=3.0, no_parts_used=True),
        job_id=str(job.id),
        request=_request(),
        current_user={
            "user_id": "user-dispatcher",
            "tenant_id": TENANT,
            "role": "dispatcher",
        },
        db=db,
    )

    entries = _entries(db, job)
    assert len(entries) == 1, f"dispatcher stacked a second row: {len(entries)}"
    assert entries[0].duration_minutes == 180, "re-closeout should restate"
    assert entries[0].user_id == USER, "hours moved off the tech who worked"
    assert _payroll_hours(db) == {USER: pytest.approx(3.0)}


def test_re_arrival_then_re_closeout_does_not_stack(db):
    """After a closeout, tapping 'I'm here' again opens a fresh timer. The next
    closeout must restate the owned row and close the new timer, not stack."""
    job, tech = _seed_job(db), _seed_technician(db)
    _seed_arrival_timer(db, job, tech)
    _closeout(db, job, hours=2.0)

    again = _seed_arrival_timer(db, job, tech, arrived_ago_hours=1.0)
    _closeout(db, job, hours=3.0)
    db.refresh(again)

    assert again.clock_out is not None, "re-arrival timer left running"
    assert _payroll_hours(db) == {USER: pytest.approx(3.0)}, "hours stacked"


def test_rate_falls_back_to_default_when_user_has_no_technician(db):
    """A closer with no Technician row (a dispatcher) has no rate to resolve."""
    from gdx_dispatch.routers.labor import DEFAULT_HOURLY_RATE

    job = _seed_job(db)  # no Technician row for USER

    _closeout(db, job, hours=1.0)

    entry = _entries(db, job)[0]
    assert float(entry.hourly_rate) == DEFAULT_HOURLY_RATE


def test_manual_stop_then_closeout_pays_only_attested_hours(db):
    """The scenario the old "Time is READ-ONLY" comments were guarding against.

    A tech arrives (timer opens), works, taps Stop on the job clock five hours
    later, then closes out attesting 2h. Two rows exist afterwards — the
    stopped timer and closeout's own — and exactly 2h may be payable.

    Counterfactual: restore the old
    ``duration_minutes = int(round(delta_seconds / 60))`` in
    mobile.py::_close_open_time_entry and this fails with 7.0 != 2.0, which is
    precisely the "an attested 2h job then bills 5h" the comment predicted.
    """
    from gdx_dispatch.routers.mobile import mobile_clock_out

    job = _seed_job(db)
    tech = _seed_technician(db)
    # Mobile's ownership gate reads jobs.assigned_to (a technician id); closeout
    # does not, which is why the older tests here don't need it.
    job.assigned_to = tech.id
    db.commit()
    _seed_arrival_timer(db, job, tech, arrived_ago_hours=5.0)

    # The tech taps Stop on the job screen.
    resp = mobile_clock_out(
        job_id=job.id.hex,
        request=_request(),
        current_user={"user_id": USER, "tenant_id": TENANT, "role": "technician"},
        db=db,
    )
    assert resp.status_code == 200
    db.commit()

    stopped = _entries(db, job)
    assert len(stopped) == 1
    assert stopped[0].clock_out is not None, "Stop must actually end the timer"
    assert stopped[0].duration_minutes == 0, (
        f"a manual stop banked {stopped[0].duration_minutes} payable minutes"
    )

    # ...then closes out, attesting 2h.
    _closeout(db, job, hours=2.0)

    assert _payroll_hours(db) == {USER: 2.0}, (
        "the tech attested 2h and must be paid for them; a stopped timer falls "
        "through to closeout's synthetic row, which carries user_id NULL and is "
        "invisible to payroll"
    )

    # ONE row, not two: closeout restates the tech's own stopped timer rather
    # than stacking a synthetic beside it.
    rows = _entries(db, job)
    assert len(rows) == 1, f"closeout stacked a second labor row: {[r.entry_type for r in rows]}"
    assert sum((r.duration_minutes or 0) for r in rows) == 120, (
        f"job costing sees {[r.duration_minutes for r in rows]} minutes; only the attested 2h counts"
    )
    # The restated row must stay findable by the re-closeout lookup, which
    # matches notes EXACTLY — otherwise a second closeout mints another row.
    assert rows[0].notes == CLOSEOUT_LABOR_NOTE


def test_manual_stop_leaves_no_open_timer_for_closeout_to_find(db):
    """Stop and closeout are two writers on one row and must agree.

    After a manual stop, closeout's `_open_job_timers` must find nothing — if
    it still saw the row it would restate it, and the elapsed span the office
    is meant to read would be overwritten.
    """
    from gdx_dispatch.routers.jobs import _open_job_timers
    from gdx_dispatch.routers.mobile import mobile_clock_out

    job = _seed_job(db)
    tech = _seed_technician(db)
    job.assigned_to = tech.id
    db.commit()
    _seed_arrival_timer(db, job, tech, arrived_ago_hours=3.0)

    mobile_clock_out(
        job_id=job.id.hex,
        request=_request(),
        current_user={"user_id": USER, "tenant_id": TENANT, "role": "technician"},
        db=db,
    )
    db.commit()

    assert _open_job_timers(db, job.id) == []
    # The span survives where a human reads it.
    assert "180" in (_entries(db, job)[0].notes or "")


def test_re_closeout_after_a_manual_stop_does_not_stack(db):
    """The restated stopped-timer row must still dedupe on a second closeout.

    `_owned_closeout_labor_entry` finds the prior row by an EXACT notes match,
    so if the restate had appended to the mobile Stop note instead of replacing
    it, this second closeout would mint another row and bill 2h + 3h = 5h for a
    3h job.
    """
    from gdx_dispatch.routers.mobile import mobile_clock_out

    job = _seed_job(db)
    tech = _seed_technician(db)
    job.assigned_to = tech.id
    db.commit()
    _seed_arrival_timer(db, job, tech, arrived_ago_hours=4.0)

    mobile_clock_out(
        job_id=job.id.hex,
        request=_request(),
        current_user={"user_id": USER, "tenant_id": TENANT, "role": "technician"},
        db=db,
    )
    db.commit()

    _closeout(db, job, hours=2.0)
    _closeout(db, job, hours=3.0)

    rows = _entries(db, job)
    assert len(rows) == 1, f"re-closeout stacked rows: {[(r.entry_type, r.duration_minutes) for r in rows]}"
    assert rows[0].duration_minutes == 180
    assert _payroll_hours(db) == {USER: 3.0}


def test_repair_tool_row_is_never_restated_by_closeout(db):
    """The /audit finding, 2026-08-25 — a prod-reachable payroll mispost.

    tools/stale_job_timer_repair.py closes an abandoned timer with
    duration_minutes = 0 — the same shape a manual mobile Stop leaves. The
    first version of _stopped_job_timer_for inferred "a tech stopped this" from
    that shape alone, so closing out one of the four repaired prod jobs (two of
    which still have no closeout) would have restated a row whose clock_in is
    in April. _close_labor_entry never moves clock_in and payroll windows on
    DATE(clock_in), so today's attested hours would post into a pay period
    already paid.

    Two guards stop it and this test proves the pair, not either one: the 24h
    restate window (which alone is enough for the four prod rows, since the
    repair tool will not touch a timer under a day old) and the Stop-marker
    predicate. `test_recent_zero_minute_row_without_the_marker_is_ignored`
    isolates the marker, because removing it does NOT turn this test red — the
    window catches the April row first.
    """
    from gdx_dispatch.routers.jobs import _stopped_job_timer_for
    from tools.stale_job_timer_repair import REPAIR_NOTE

    job = _seed_job(db)
    tech = _seed_technician(db)
    timer = _seed_arrival_timer(db, job, tech, arrived_ago_hours=24 * 118)
    # Exactly what the repair tool leaves behind.
    timer.clock_out = datetime.now(UTC)
    timer.duration_minutes = 0
    timer.notes = f"{REPAIR_NOTE}; ran 169961 min (118.0 d), not attested"
    db.commit()

    assert _stopped_job_timer_for(db, job.id, USER) is None, (
        "closeout would have restated a repair-tool row and posted today's "
        "hours into an April pay period"
    )

    _closeout(db, job, hours=2.0)

    rows = _entries(db, job)
    repaired = [r for r in rows if (r.notes or "").startswith(REPAIR_NOTE)]
    assert len(repaired) == 1, "the repaired row must survive untouched"
    assert repaired[0].duration_minutes == 0, "the repaired row must stay unpaid"
    assert REPAIR_NOTE in (repaired[0].notes or ""), "its evidence must survive"


def test_stale_mobile_stop_is_not_restated_and_says_so(db, caplog):
    """Even a genuine Stop stops being safe to restate in another pay period."""
    from gdx_dispatch.routers.jobs import _stopped_job_timer_for
    from gdx_dispatch.routers.mobile import MOBILE_STOP_LABOR_NOTE

    job = _seed_job(db)
    tech = _seed_technician(db)
    timer = _seed_arrival_timer(db, job, tech, arrived_ago_hours=24 * 9)
    timer.clock_out = datetime.now(UTC)
    timer.duration_minutes = 0
    timer.notes = f"{MOBILE_STOP_LABOR_NOTE}; elapsed 40 min, not attested"
    db.commit()

    with caplog.at_level("WARNING"):
        assert _stopped_job_timer_for(db, job.id, USER) is None
    assert "closeout_stale_stopped_timer_not_restated" in caplog.text, (
        "falling through must be loud — the tech loses the hours otherwise"
    )


def test_fresh_mobile_stop_is_restated(db):
    """The case the function exists for still works after the narrowing."""
    from gdx_dispatch.routers.jobs import _stopped_job_timer_for
    from gdx_dispatch.routers.mobile import MOBILE_STOP_LABOR_NOTE

    job = _seed_job(db)
    tech = _seed_technician(db)
    timer = _seed_arrival_timer(db, job, tech, arrived_ago_hours=3.0)
    timer.clock_out = datetime.now(UTC)
    timer.duration_minutes = 0
    timer.notes = f"{MOBILE_STOP_LABOR_NOTE}; elapsed 180 min, not attested"
    db.commit()

    assert _stopped_job_timer_for(db, job.id, USER) is not None


def test_recent_zero_minute_row_without_the_marker_is_ignored(db):
    """Isolates the Stop-marker predicate from the 24h window.

    A closed, zero-minute, same-user row on this job from two hours ago that
    the Stop button did NOT write — any future writer of that shape. The window
    lets it through, so only the marker can reject it.

    Counterfactual: drop `TimeEntry.notes.like(...)` from
    _stopped_job_timer_for and this fails, because the row is inside the
    window and matches on every other predicate.
    """
    from gdx_dispatch.routers.jobs import _stopped_job_timer_for

    job = _seed_job(db)
    tech = _seed_technician(db)
    timer = _seed_arrival_timer(db, job, tech, arrived_ago_hours=2.0)
    timer.clock_out = datetime.now(UTC)
    timer.duration_minutes = 0
    timer.notes = "Closed by some other process"
    db.commit()

    assert _stopped_job_timer_for(db, job.id, USER) is None, (
        "only rows the mobile Stop path wrote are ours to restate"
    )

