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

Payroll assertions run payroll's OWN query (`_fetch_tech_hours`), not a
paraphrase of it — an earlier version of this file hand-rolled the filter and
was structurally blind to the bugs above.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
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
from gdx_dispatch.routers.jobs import CloseoutPayload, closeout_job

TENANT = "tenant-1"
USER = "user-michael"
RATE = 42.5


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
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
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
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
    job = Job(
        customer_id=uuid4(),
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
