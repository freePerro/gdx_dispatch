"""Multi-day jobs PR 1: a job's other visit days survive the sync, and
"I'm here" finds the right visit (multi-day-jobs-plan §5.2, §5.4 B2).

The sync used to own every live appointment on the job, keyed on (job, tech):
a second day for the same tech was retired on the next job edit, and a
closed day was dragged to the new date. Arrival loaded "the" appointment with
``scalar_one_or_none()``, which raises once a job holds two.

Dates are fixed at noon UTC so no case straddles the shop's midnight.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models.tenant_models  # noqa: F401
from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models.tenant_models import Appointment, Customer, Job, Technician
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers.jobs import _set_job_assignments, _sync_job_appointment
from gdx_dispatch.tests.test_mobile_state_machine import TECH, TENANT, USER, app_and_db  # noqa: F401

DAY1 = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # a Monday, 09:00 Chicago
DAY2 = DAY1 + timedelta(days=1)
DAY3 = DAY1 + timedelta(days=2)
USER_CTX = {"sub": "user-1"}


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    sess = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield sess
    sess.close()
    engine.dispose()


def _job(db, scheduled_at=DAY1, assigned_to="tech-1") -> Job:
    job = Job(
        id=uuid.uuid4(), title="Install 16x7", company_id="tenant-test",
        scheduled_at=scheduled_at, status="Scheduled", priority="Normal",
        job_type="Install", lifecycle_stage="scheduled", assigned_to=assigned_to,
        dispatch_status="assigned", billing_status="unbilled", is_demo=False,
        is_return_visit=False, created_at=DAY1, updated_at=DAY1,
    )
    db.add(job)
    db.flush()
    return job


def _visit(db, job, start, tech="tech-1", status="scheduled") -> Appointment:
    appt = Appointment(
        id=uuid4(), company_id="tenant-test", job_id=job.id, tech_id=tech,
        title=job.title, start_at=start, end_at=start + timedelta(hours=8),
        status=status, created_at=DAY1 - timedelta(days=7),
    )
    db.add(appt)
    db.flush()
    return appt


def _live(db, job) -> list[Appointment]:
    return db.execute(
        select(Appointment).where(
            Appointment.job_id == job.id, Appointment.deleted_at.is_(None),
        ).order_by(Appointment.start_at, Appointment.tech_id)
    ).scalars().all()


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _three_day_job(db) -> tuple[Job, list[Appointment]]:
    job = _job(db)
    visits = [_visit(db, job, d) for d in (DAY1, DAY2, DAY3)]
    db.commit()
    return job, visits


def _edit(db, job, **changes):
    """What update_job does: remember the date, apply, sync."""
    previous = job.scheduled_at
    for k, v in changes.items():
        setattr(job, k, v)
    db.flush()
    _sync_job_appointment(db, job, "tenant-test", USER_CTX, previous_scheduled_at=previous)
    db.commit()


# ── The sync (B1) ─────────────────────────────────────────────────────


def test_title_edit_keeps_all_three_days(db):
    job, visits = _three_day_job(db)
    _edit(db, job, title="Install 16x7 — two doors")
    live = _live(db, job)
    assert [v.id for v in live] == [v.id for v in visits]
    assert [_utc(v.start_at) for v in live] == [DAY1, DAY2, DAY3]
    # Only the primary day's visit carries the sync's write.
    assert live[0].title == "Install 16x7 — two doors"
    assert live[1].title == "Install 16x7"


def test_time_change_on_day_one_moves_only_day_one(db):
    job, _ = _three_day_job(db)
    _edit(db, job, scheduled_at=DAY1 + timedelta(hours=2))
    assert [_utc(v.start_at) for v in _live(db, job)] == [
        DAY1 + timedelta(hours=2), DAY2, DAY3,
    ]


def test_tech_change_retires_only_the_primary_day(db):
    job, visits = _three_day_job(db)
    _set_job_assignments(
        db, job_id=str(job.id), tech_ids=["tech-2"], lead_tech_id="tech-2", user_id="user-1",
    )
    db.refresh(job)
    _edit(db, job)
    live = _live(db, job)
    assert [(v.tech_id, _utc(v.start_at)) for v in live] == [
        ("tech-2", DAY1), ("tech-1", DAY2), ("tech-1", DAY3),
    ]
    assert visits[0].deleted_at is not None


def test_date_move_on_day_one_leaves_other_days(db):
    job, visits = _three_day_job(db)
    day5 = DAY1 + timedelta(days=4)
    _edit(db, job, scheduled_at=day5)
    live = _live(db, job)
    assert [_utc(v.start_at) for v in live] == [DAY2, DAY3, day5]
    assert visits[0] in live  # moved in place, not re-created


def test_a_completed_visit_is_never_moved_or_retired(db):
    job = _job(db)
    done = _visit(db, job, DAY1, status="completed")
    db.commit()
    _edit(db, job, scheduled_at=DAY2)
    live = _live(db, job)
    assert done in live and _utc(done.start_at) == DAY1
    # No open visit on the old day: a new one is inserted for the new day.
    new = [v for v in live if v.id != done.id]
    assert len(new) == 1 and _utc(new[0].start_at) == DAY2
    assert new[0].status == "scheduled"


def test_a_cancelled_visit_is_left_alone_too(db):
    job = _job(db)
    cancelled = _visit(db, job, DAY1, status="cancelled")
    db.commit()
    _edit(db, job, scheduled_at=DAY1 + timedelta(hours=1))
    assert _utc(cancelled.start_at) == DAY1 and cancelled.deleted_at is None
    # Same day: the cancel stands, no fresh visit beside it.
    assert [v.id for v in _live(db, job)] == [cancelled.id]


def test_a_worked_day_survives_a_date_move_onto_the_next_day(db):
    # Prod's tech flow leaves an arrived visit "scheduled"; it is still a
    # worked day, never merged away.
    job = _job(db)
    worked = _visit(db, job, DAY1)
    worked.arrived_at = DAY1
    day2 = _visit(db, job, DAY2)
    db.commit()
    _edit(db, job, scheduled_at=DAY2 + timedelta(hours=1))
    assert [v.id for v in _live(db, job)] == [worked.id, day2.id]
    assert _utc(worked.start_at) == DAY1


def test_a_worked_single_day_job_moved_gets_a_new_visit(db):
    # "Needs another day": the worked visit stays put with its arrival, and
    # the new day gets a fresh visit "I'm here" can stamp.
    job = _job(db)
    worked = _visit(db, job, DAY1)
    worked.arrived_at = DAY1
    db.commit()
    _edit(db, job, scheduled_at=DAY3)
    live = _live(db, job)
    assert _utc(worked.start_at) == DAY1 and worked in live
    new = [v for v in live if v.id != worked.id]
    assert len(new) == 1 and _utc(new[0].start_at) == DAY3 and new[0].arrived_at is None


def test_a_date_move_onto_a_cancelled_day_rebooks_it(db):
    job = _job(db)
    _visit(db, job, DAY1, status="completed")
    _visit(db, job, DAY2, status="cancelled")
    db.commit()
    _edit(db, job, scheduled_at=DAY2)
    assert [_utc(v.start_at) for v in _live(db, job) if v.status == "scheduled"] == [DAY2]


def test_clear_then_redate_onto_a_cancelled_day_rebooks_it(db):
    job = _job(db)
    _visit(db, job, DAY1, status="cancelled")
    db.commit()
    _edit(db, job, scheduled_at=None)
    _edit(db, job, scheduled_at=DAY1 + timedelta(hours=2))
    assert [_utc(v.start_at) for v in _live(db, job) if v.status == "scheduled"] == [
        DAY1 + timedelta(hours=2)
    ]


def test_clear_then_redate_the_same_afternoon_books_a_return(db):
    job = _job(db)
    worked = _visit(db, job, DAY1)
    worked.arrived_at = DAY1
    db.commit()
    _edit(db, job, scheduled_at=None)
    _edit(db, job, scheduled_at=DAY1 + timedelta(hours=5))
    assert _utc(worked.start_at) == DAY1
    new = [v for v in _live(db, job) if v.id != worked.id]
    assert len(new) == 1 and new[0].arrived_at is None


def _crew_with_a_cancelled_helper_and_a_dragged_lead(db):
    job = _job(db)
    _set_job_assignments(
        db, job_id=str(job.id), tech_ids=["tech-1", "tech-2"], lead_tech_id="tech-1",
        user_id="user-1",
    )
    db.refresh(job)
    _visit(db, job, DAY1, tech="tech-2", status="cancelled")
    dragged = _visit(db, job, DAY2, tech="tech-1")  # dragged on the calendar
    db.commit()
    return job, dragged


def test_a_cancelled_visit_keeps_drift_handling_on_a_title_edit(db):
    job, dragged = _crew_with_a_cancelled_helper_and_a_dragged_lead(db)
    _edit(db, job, title="typo fixed")
    lead = [v for v in _live(db, job) if v.tech_id == "tech-1"]
    assert [v.id for v in lead] == [dragged.id]
    assert _utc(dragged.start_at) == DAY1


def test_a_cancelled_visit_keeps_drift_handling_on_a_date_move(db):
    job, dragged = _crew_with_a_cancelled_helper_and_a_dragged_lead(db)
    _edit(db, job, scheduled_at=DAY3)
    lead = [v for v in _live(db, job) if v.tech_id == "tech-1"]
    assert [v.id for v in lead] == [dragged.id]
    assert _utc(dragged.start_at) == DAY3


def test_a_cancelled_day_one_keeps_the_rebooked_day_two(db):
    # Rained out Monday: the office cancelled it and booked Tuesday on the
    # Appointments page; the job still says Monday. A title edit must not
    # drag Tuesday back onto Monday, nor book Monday again.
    job = _job(db)
    _visit(db, job, DAY1, status="cancelled")
    rebooked = _visit(db, job, DAY2)
    db.commit()
    _edit(db, job, title="typo fixed")
    open_ = [v for v in _live(db, job) if v.status != "cancelled"]
    assert [v.id for v in open_] == [rebooked.id]
    assert _utc(rebooked.start_at) == DAY2


def test_a_status_only_arrival_counts_as_a_worked_day(db):
    # The Appointments-page PATCH can set status "arrived" with no time.
    job = _job(db)
    marked = _visit(db, job, DAY1, status="arrived")
    db.commit()
    _edit(db, job, title="typo fixed")
    assert [v.id for v in _live(db, job)] == [marked.id]
    _edit(db, job, scheduled_at=DAY3)
    assert _utc(marked.start_at) == DAY1


def test_a_time_fix_after_arrival_books_nothing_new(db):
    job = _job(db)
    worked = _visit(db, job, DAY1)
    worked.arrived_at = DAY1
    db.commit()
    _edit(db, job, scheduled_at=DAY1 + timedelta(hours=1))
    assert [v.id for v in _live(db, job)] == [worked.id]


def test_a_removed_tech_loses_a_drifted_visit_on_a_single_day_job(db):
    job = _job(db)
    _visit(db, job, DAY2, tech="tech-1")  # dragged on the calendar
    _set_job_assignments(
        db, job_id=str(job.id), tech_ids=["tech-2"], lead_tech_id="tech-2", user_id="user-1",
    )
    db.commit()
    db.refresh(job)
    _edit(db, job)
    assert [(v.tech_id, _utc(v.start_at)) for v in _live(db, job)] == [("tech-2", DAY1)]


def test_clearing_the_date_keeps_a_worked_visit(db):
    job = _job(db)
    worked = _visit(db, job, DAY1)
    worked.arrived_at = DAY1
    db.commit()
    _edit(db, job, scheduled_at=None)
    assert [v.id for v in _live(db, job)] == [worked.id]


def test_a_title_edit_beside_a_completed_visit_inserts_nothing(db):
    job = _job(db)
    done = _visit(db, job, DAY1, status="completed")
    db.commit()
    _edit(db, job, title="typo fixed")
    assert [v.id for v in _live(db, job)] == [done.id]


def test_a_cancelled_crew_visit_is_not_resurrected_by_a_title_edit(db):
    job = _job(db)
    _visit(db, job, DAY1, tech="tech-1")
    gone = _visit(db, job, DAY1, tech="tech-2", status="cancelled")
    _set_job_assignments(
        db, job_id=str(job.id), tech_ids=["tech-1", "tech-2"], lead_tech_id="tech-1", user_id="user-1",
    )
    db.commit()
    db.refresh(job)
    _edit(db, job, title="title only")
    tech2 = [v for v in _live(db, job) if v.tech_id == "tech-2"]
    assert [v.id for v in tech2] == [gone.id] and gone.status == "cancelled"


def test_collision_merges_into_the_existing_day_with_a_trail(db):
    job, visits = _three_day_job(db)
    _edit(db, job, scheduled_at=DAY2 + timedelta(hours=1))
    live = _live(db, job)
    assert [v.id for v in live] == [visits[1].id, visits[2].id]
    assert _utc(visits[1].start_at) == DAY2 + timedelta(hours=1)
    assert visits[0].deleted_at is not None
    row = db.execute(
        select(AuditLog).where(AuditLog.action == "visit_merged")
    ).scalar_one()
    assert row.entity_id == str(job.id)
    assert row.details["retired_visit_id"] == str(visits[0].id)
    assert row.details["kept_visit_id"] == str(visits[1].id)


def test_clearing_the_date_retires_only_open_visits(db):
    job = _job(db)
    done = _visit(db, job, DAY1, status="completed")
    open_two = _visit(db, job, DAY2)
    open_three = _visit(db, job, DAY3)
    db.commit()
    _edit(db, job, scheduled_at=None)
    db.expire_all()
    assert [v.id for v in _live(db, job)] == [done.id]
    assert open_two.deleted_at is not None and open_three.deleted_at is not None


def test_duplicates_on_one_day_keep_the_oldest(db):
    job = _job(db)
    older = _visit(db, job, DAY1)
    newer = _visit(db, job, DAY1)
    newer.created_at = DAY1
    db.commit()
    _edit(db, job, title="t")
    assert [v.id for v in _live(db, job)] == [older.id]


def test_a_single_visit_that_drifted_is_moved_not_duplicated(db):
    # An Appointments-page edit moved the visit; the job still says DAY1.
    job = _job(db)
    drifted = _visit(db, job, DAY2)
    db.commit()
    _edit(db, job, title="edited")
    live = _live(db, job)
    assert [v.id for v in live] == [drifted.id]
    assert _utc(drifted.start_at) == DAY1  # pulled back, as the old sync did


def test_a_date_written_without_the_sync_still_reschedules_cleanly(db):
    # /uncomplete and /reactivate write scheduled_at without syncing.
    job = _job(db)
    visit = _visit(db, job, DAY1)
    db.commit()
    job.scheduled_at = DAY2
    db.commit()
    _edit(db, job, scheduled_at=DAY3)
    assert [v.id for v in _live(db, job)] == [visit.id]
    assert _utc(visit.start_at) == DAY3


def test_a_continuing_job_inserts_rather_than_moving_its_next_day(db):
    # Day 1 closed, day 2 booked: moving the primary day must not drag day 2.
    job = _job(db)
    _visit(db, job, DAY1, status="completed")
    day_two = _visit(db, job, DAY2)
    db.commit()
    day5 = DAY1 + timedelta(days=4)
    _edit(db, job, scheduled_at=day5)
    starts = sorted(_utc(v.start_at) for v in _live(db, job))
    assert starts == [DAY1, DAY2, day5]
    assert _utc(day_two.start_at) == DAY2


def test_a_helper_booked_for_day_two_only_keeps_that_visit(db):
    job = _job(db)
    for d in (DAY1, DAY2, DAY3):
        _visit(db, job, d, tech="tech-1")
    helper = _visit(db, job, DAY2, tech="tech-2")
    _set_job_assignments(
        db, job_id=str(job.id), tech_ids=["tech-1", "tech-2"], lead_tech_id="tech-1", user_id="user-1",
    )
    db.commit()
    db.refresh(job)
    _edit(db, job, title="title only")
    assert _utc(helper.start_at) == DAY2 and helper.deleted_at is None
    # The sync gives tech-2 a day-1 visit of its own: tech-2 is on the job's
    # crew. Whether a day-2-only helper belongs on the crew is PR 2's call.
    assert sorted((v.tech_id, _utc(v.start_at)) for v in _live(db, job)) == [
        ("tech-1", DAY1), ("tech-1", DAY2), ("tech-1", DAY3), ("tech-2", DAY1), ("tech-2", DAY2),
    ]


# ── Arrival (B2) ──────────────────────────────────────────────────────


def _seed_arrival(db, *, other_tech_today=False, yesterday=False):
    db.add(Technician(id=TECH, company_id=TENANT, user_id=USER, active=True))
    cust = Customer(id=uuid4(), name="Acme", phone="555", email="a@x.com",
                    address="11 Main", company_id=TENANT)
    db.add(cust)
    now = datetime.now(UTC)
    job = Job(id=uuid4(), company_id=TENANT, customer_id=cust.id, title="Fix",
              description="", scheduled_at=now, assigned_to=TECH,
              dispatch_status="en_route")
    db.add(job)

    def add(tech, start):
        a = Appointment(id=uuid4(), company_id=TENANT, job_id=job.id,
                        customer_id=cust.id, tech_id=tech, title="Fix",
                        start_at=start, end_at=start + timedelta(hours=1))
        db.add(a)
        return a

    mine = add(TECH, now)
    other = add("tech-2", now) if other_tech_today else None
    old = add(TECH, now - timedelta(days=1)) if yesterday else None
    db.commit()
    return job, mine, other, old


def test_arrival_on_a_two_tech_job_stamps_this_techs_visit(app_and_db):  # noqa: F811
    client, db = app_and_db
    job, mine, other, _ = _seed_arrival(db, other_tech_today=True)
    r = client.post(f"/api/mobile/jobs/{job.id.hex}/arrived")
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(Appointment, mine.id).arrived_at is not None
    assert db.get(Appointment, other.id).arrived_at is None


def test_arrival_with_a_visit_on_another_day_stamps_today(app_and_db):  # noqa: F811
    client, db = app_and_db
    job, mine, _, old = _seed_arrival(db, yesterday=True)
    r = client.post(f"/api/mobile/jobs/{job.id.hex}/arrived")
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(Appointment, mine.id).arrived_at is not None
    assert db.get(Appointment, old.id).arrived_at is None


def test_arrival_visit_stamps_no_visit_on_a_multi_visit_job_with_none_today(db):
    # Any pick would mark the wrong day worked.
    job = _job(db)
    _visit(db, job, DAY1 - timedelta(days=1))
    _visit(db, job, DAY1)
    _visit(db, job, DAY3)
    db.commit()
    assert mobile_router._arrival_visit(db, job.id, "tech-1", DAY2) is None


def test_arrival_visit_never_stamps_a_future_day_of_a_multi_day_job(db):
    job = _job(db)
    _visit(db, job, DAY2)
    _visit(db, job, DAY3)
    db.commit()
    assert mobile_router._arrival_visit(db, job.id, "tech-1", DAY1) is None


@pytest.mark.parametrize("today_visit", [
    ("tech-1", "cancelled"),  # rained out, tapped anyway
    ("tech-2", "scheduled"),  # today is someone else's
])
def test_arrival_visit_never_stamps_a_future_day_beside_another_visit(db, today_visit):
    job = _job(db)
    tech, status = today_visit
    _visit(db, job, DAY1, tech=tech, status=status)
    _visit(db, job, DAY3, tech="tech-1")
    db.commit()
    assert mobile_router._arrival_visit(db, job.id, "tech-1", DAY1) is None


def test_arrival_visit_never_takes_another_techs_future_visit(db):
    job = _job(db)
    _visit(db, job, DAY1, status="completed")
    _visit(db, job, DAY3, tech="tech-2")
    db.commit()
    assert mobile_router._arrival_visit(db, job.id, "tech-1", DAY1) is None


def _arrive(db, job, when):
    """What the arrival route does with the visit it picks."""
    visit = mobile_router._arrival_visit(db, job.id, "tech-1", when)
    moved = mobile_router._stamp_arrival(db, visit, job, when) if visit is not None else None
    db.commit()
    return visit, moved


def test_an_early_arrival_moves_the_visit_and_the_job_to_the_day_worked(db):
    job = _job(db, scheduled_at=DAY3)
    only = _visit(db, job, DAY3)
    db.commit()
    arrived = DAY1 + timedelta(hours=1)
    visit, moved = _arrive(db, job, arrived)
    assert visit.id == only.id
    assert _utc(only.start_at) == arrived and _utc(job.scheduled_at) == arrived
    assert moved["visit_id"] == str(only.id)
    # A later office edit books nothing for Wednesday.
    _edit(db, job, title="typo fixed")
    assert [v.id for v in _live(db, job)] == [only.id]


def test_a_drifted_job_arrival_then_a_title_edit_books_nothing_new(db):
    # /reactivate moved the job to day 2 without the sync; the visit is day 1.
    job = _job(db, scheduled_at=DAY2)
    stale = _visit(db, job, DAY1)
    db.commit()
    _arrive(db, job, DAY2 + timedelta(hours=1))
    _edit(db, job, title="typo fixed")
    assert [v.id for v in _live(db, job)] == [stale.id]
    assert _utc(stale.start_at) == DAY2 + timedelta(hours=1)


def test_a_date_move_onto_a_worked_day_books_nothing(db):
    job = _job(db, scheduled_at=DAY3)
    worked = _visit(db, job, DAY1)
    worked.arrived_at = DAY1
    db.commit()
    _edit(db, job, scheduled_at=DAY1 + timedelta(hours=2))
    assert [v.id for v in _live(db, job)] == [worked.id]


def test_a_date_move_onto_a_worked_day_retires_the_moved_visit(db):
    # Worked day 1, open visit on day 3; the office moves the job back to
    # day 1. Day 1 keeps its one worked visit, and the retire is audited.
    job = _job(db, scheduled_at=DAY3)
    worked = _visit(db, job, DAY1)
    worked.arrived_at = DAY1
    moved = _visit(db, job, DAY3)
    db.commit()
    _edit(db, job, scheduled_at=DAY1 + timedelta(hours=2))
    assert [v.id for v in _live(db, job)] == [worked.id]
    assert _utc(worked.start_at) == DAY1
    trail = db.execute(
        select(AuditLog).where(AuditLog.action == "visit_merged")
    ).scalars().all()
    assert len(trail) == 1
    assert trail[0].details["retired_visit_id"] == str(moved.id)
    assert trail[0].details["kept_visit_id"] == str(worked.id)


def test_arrival_never_moves_a_status_only_arrived_visit(db):
    # The office marked Monday's only visit "arrived" (no time) through the
    # Appointments-page PATCH; a Wednesday tap must not move Monday away.
    job = _job(db)
    marked = _visit(db, job, DAY1, status="arrived")
    db.commit()
    visit, moved = _arrive(db, job, DAY3 + timedelta(hours=1))
    assert visit is None and moved is None
    assert _utc(marked.start_at) == DAY1 and _utc(job.scheduled_at) == DAY1


def test_an_arrival_on_the_visit_day_moves_nothing(db):
    job = _job(db)
    only = _visit(db, job, DAY1)
    db.commit()
    _, moved = _arrive(db, job, DAY1 + timedelta(minutes=20))
    assert moved is None and _utc(only.start_at) == DAY1 and _utc(job.scheduled_at) == DAY1


def test_arrival_visit_takes_a_single_visit_job_early(db):
    job = _job(db)
    only = _visit(db, job, DAY3)
    db.commit()
    assert mobile_router._arrival_visit(db, job.id, "tech-1", DAY1).id == only.id


def test_arrival_visit_takes_a_same_day_return_over_the_worked_visit(db):
    job = _job(db)
    morning = _visit(db, job, DAY1)
    morning.arrived_at = DAY1
    ret = _visit(db, job, DAY1 + timedelta(hours=5))
    db.commit()
    picked = mobile_router._arrival_visit(db, job.id, "tech-1", DAY1 + timedelta(hours=5))
    assert picked.id == ret.id


def test_arrival_visit_never_stamps_a_closed_visit(db):
    job = _job(db)
    _visit(db, job, DAY1, status="cancelled")
    _visit(db, job, DAY1 - timedelta(days=1), status="completed")
    db.commit()
    assert mobile_router._arrival_visit(db, job.id, "tech-1", DAY1) is None


def test_arrival_visit_skips_other_techs_when_the_job_has_several(db):
    job = _job(db)
    _visit(db, job, DAY1, tech="tech-2")
    _visit(db, job, DAY1, tech="tech-3")
    db.commit()
    assert mobile_router._arrival_visit(db, job.id, "tech-1", DAY1) is None


def test_arrival_visit_takes_the_only_visit_after_a_tech_swap(db):
    # /uncomplete with assigned_to changes the job's tech, not the visit's.
    job = _job(db)
    only = _visit(db, job, DAY1, tech="tech-2")
    db.commit()
    assert mobile_router._arrival_visit(db, job.id, "tech-1", DAY1).id == only.id
