"""Multi-day jobs: one job, many visit days (multi-day-jobs-plan §5.2a).

Row pins: one test per row of the state table (R0–R4, E1–E6, C1–C7, F1, X1,
A1–A3), each asserting its exact result against a real database through the
planner (``plan_for_job``), the applier (``apply_visit_plan``) and
``recompute_job_schedule`` — the same three calls every writer makes. The
whole grid runs the planner alone in ``test_visit_sync_grid.py``; these prove
the rows survive the trip through the ORM, the audit log and invariant I.

Dates are fixed at noon-ish UTC so no case straddles the shop's midnight.
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
from gdx_dispatch.services.visit_sync import (
    UNSET,
    apply_visit_plan,
    job_crew,
    job_visit_fields,
    plan_for_job,
    recompute_job_schedule,
    shop_day,
    shop_tz,
)
from gdx_dispatch.tests.test_mobile_state_machine import TECH, TENANT, USER, app_and_db  # noqa: F401

DAY1 = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # a Monday, 09:00 Chicago
DAY2 = DAY1 + timedelta(days=1)
DAY3 = DAY1 + timedelta(days=2)
DAY4 = DAY1 + timedelta(days=3)


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


def _job(db, scheduled_at=DAY1, assigned_to="tech-1", stage="scheduled") -> Job:
    job = Job(
        id=uuid.uuid4(), title="Install 16x7", company_id="tenant-test",
        scheduled_at=scheduled_at, status="Scheduled", priority="Normal",
        job_type="Install", lifecycle_stage=stage, assigned_to=assigned_to,
        dispatch_status="assigned", billing_status="unbilled", is_demo=False,
        is_return_visit=False, created_at=DAY1, updated_at=DAY1,
    )
    db.add(job)
    db.flush()
    return job


def _visit(db, job, start, tech="tech-1", status="scheduled", arrived=None) -> Appointment:
    appt = Appointment(
        id=uuid4(), company_id="tenant-test", job_id=job.id, tech_id=tech,
        title=job.title, start_at=start, end_at=start + timedelta(hours=8),
        status=status, arrived_at=arrived, created_at=DAY1 - timedelta(days=7),
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


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _layout(db, job) -> list[tuple]:
    return [(v.tech_id, _utc(v.start_at), v.status) for v in _live(db, job)]


def _actions(db, job) -> list[str]:
    return [
        r.action for r in db.execute(
            select(AuditLog).where(AuditLog.entity_id == str(job.id)).order_by(AuditLog.id)
        ).scalars().all()
    ]


def _edit(db, job, *, crew_before=None, **kwargs):
    """What a writer does: plan, refuse or apply, then recompute.

    Returns the refusal, or None when the edit was applied."""
    plan = plan_for_job(db, job, crew_before=crew_before, **kwargs)
    if plan.refusal is not None:
        db.rollback()
        return plan.refusal
    if plan.scheduled_at is not UNSET:
        job.scheduled_at = plan.scheduled_at
    apply_visit_plan(db, job, plan, "user-1")
    # The applier writes what the planner planned, row for row: the grid
    # proves the plan, this proves the plan is what lands (an action on a
    # row inserted earlier in the same plan was once dropped here).
    assert sorted(
        (v.tech_id or "", _utc(v.start_at), _utc(v.end_at), v.status) for v in _live(db, job)
    ) == sorted(
        (v.tech_id or "", _utc(v.start_at), _utc(v.end_at), v.status) for v in plan.visits
    ), "the DB does not hold the rows the plan left"
    recompute_job_schedule(db, job, "user-1", "test")
    db.commit()
    return None


def _three_day_job(db) -> tuple[Job, list[Appointment]]:
    job = _job(db)
    visits = [_visit(db, job, d) for d in (DAY1, DAY2, DAY3)]
    db.commit()
    return job, visits


def _assert_unchanged(db, job, layout, actions, scheduled_at):
    db.expire_all()
    assert _layout(db, job) == layout
    assert _actions(db, job) == actions
    assert _utc(db.get(Job, job.id).scheduled_at) == scheduled_at


# ── Job edit: E1–E6 ───────────────────────────────────────────────────


def test_e1_a_title_edit_copies_onto_open_visits_and_books_nothing(db):
    job, visits = _three_day_job(db)
    job.title = "Install 16x7 — renamed"
    assert _edit(db, job, scheduled_at=DAY1, fields=job_visit_fields(db, job)) is None
    assert [v.title for v in _live(db, job)] == ["Install 16x7 — renamed"] * 3
    assert [v.id for v in _live(db, job)] == [v.id for v in visits]
    assert _actions(db, job) == ["visit_updated"] * 3


def test_e1_a_resave_never_recreates_a_deleted_visit(db):
    job = _job(db)
    gone = _visit(db, job, DAY1)
    gone.deleted_at = DAY1
    db.commit()
    assert _edit(db, job, scheduled_at=DAY1, fields=job_visit_fields(db, job)) is None
    assert _live(db, job) == []


def test_e2_a_time_change_moves_only_the_current_day(db):
    job, visits = _three_day_job(db)
    later = DAY1 + timedelta(hours=2)
    assert _edit(db, job, scheduled_at=later) is None
    assert [_utc(v.start_at) for v in _live(db, job)] == [later, DAY2, DAY3]
    assert _utc(job.scheduled_at) == later
    assert "visit_moved" in _actions(db, job)


def test_e2_moves_every_open_visit_on_n_including_unassigned_and_outsiders(db):
    job = _job(db)
    _visit(db, job, DAY1, tech="tech-1")
    _visit(db, job, DAY1, tech=None)
    _visit(db, job, DAY1, tech="tech-outside")
    db.commit()
    assert _edit(db, job, scheduled_at=DAY3) is None
    assert {(t, s) for t, s, _ in _layout(db, job)} == {
        ("tech-1", DAY3), (None, DAY3), ("tech-outside", DAY3),
    }


def test_e2_minute_truncates_the_typed_value(db):
    job = _job(db)
    _visit(db, job, DAY1)
    db.commit()
    typed = DAY1 + timedelta(hours=1, seconds=31, microseconds=123)
    assert _edit(db, job, scheduled_at=typed) is None
    assert _utc(job.scheduled_at) == DAY1 + timedelta(hours=1)
    assert _utc(_live(db, job)[0].start_at) == DAY1 + timedelta(hours=1)


def test_e3_closed_and_cancelled_visits_on_n_stay_where_they_are(db):
    job = _job(db)
    done = _visit(db, job, DAY1, tech="tech-2", status="completed")
    called_off = _visit(db, job, DAY1, tech="tech-3", status="cancelled")
    _visit(db, job, DAY1)
    db.commit()
    assert _edit(db, job, scheduled_at=DAY2) is None
    db.expire_all()
    assert _utc(db.get(Appointment, done.id).start_at) == DAY1
    assert _utc(db.get(Appointment, called_off.id).start_at) == DAY1
    assert ("tech-1", DAY2, "scheduled") in _layout(db, job)


def test_e4_a_date_set_with_no_current_day_books_each_crew_tech(db):
    job = _job(db, scheduled_at=None)
    db.commit()
    assert _edit(db, job, scheduled_at=DAY2, fields=job_visit_fields(db, job)) is None
    assert _layout(db, job) == [("tech-1", DAY2, "scheduled")]
    assert _utc(job.scheduled_at) == DAY2
    # Recompute finds scheduled_at already equal to the new visit's start, so
    # it writes no row of its own.
    assert _actions(db, job) == ["visit_added"]


def test_e4_a_return_after_every_day_closed_books_a_new_visit(db):
    job = _job(db)
    _visit(db, job, DAY1, status="completed", arrived=DAY1)
    db.commit()
    assert _edit(db, job, scheduled_at=DAY3) is None
    assert _layout(db, job)[-1] == ("tech-1", DAY3, "scheduled")


def test_e5_a_time_fix_after_the_day_closed_books_no_second_visit(db):
    job = _job(db)
    _visit(db, job, DAY1, status="completed", arrived=DAY1)
    db.commit()
    assert _edit(db, job, scheduled_at=DAY1 + timedelta(hours=1)) is None
    assert len(_live(db, job)) == 1


def test_e6_clearing_the_date_retires_every_open_visit_and_keeps_worked_ones(db):
    job = _job(db)
    worked = _visit(db, job, DAY1 - timedelta(days=1), status="completed", arrived=DAY1)
    _visit(db, job, DAY1)
    _visit(db, job, DAY2)
    db.commit()
    assert _edit(db, job, scheduled_at=None) is None
    assert [v.id for v in _live(db, job)] == [worked.id]
    assert _actions(db, job).count("visit_retired") == 2


# ── Refusals: R1–R4, nothing written ──────────────────────────────────


def _refused(db, job, code, **kwargs):
    layout, actions, stored = _layout(db, job), _actions(db, job), _utc(job.scheduled_at)
    refusal = _edit(db, job, **kwargs)
    assert refusal is not None and refusal.code == code, refusal
    _assert_unchanged(db, job, layout, actions, stored)
    return refusal


def test_r1_day_one_cannot_move_onto_or_past_day_two(db):
    job, _ = _three_day_job(db)
    _refused(db, job, "onto_booked_day", scheduled_at=DAY2)
    _refused(db, job, "onto_booked_day", scheduled_at=DAY4)


def test_r2_a_tech_with_a_live_visit_on_the_target_day_is_not_double_booked(db):
    job = _job(db)
    _visit(db, job, DAY1)
    _visit(db, job, DAY3, status="completed", arrived=DAY3)
    db.commit()
    _refused(db, job, "double_booked", scheduled_at=DAY3)


def test_r2_counts_a_tech_added_in_the_same_save(db):
    job = _job(db)
    _visit(db, job, DAY1)
    _visit(db, job, DAY3, tech="tech-2", status="completed", arrived=DAY3)
    db.commit()
    _refused(
        db, job, "double_booked", scheduled_at=DAY3,
        crew_before=["tech-1"], crew_after=("tech-1", "tech-2"),
    )


def test_r2_second_clause_a_crew_change_that_leaves_nothing_to_land_on_t(db):
    # Found by the whole grid: removing the only tech on N while they already
    # hold a visit on T books nobody on T, so the job would read a day the
    # office did not type.
    job = _job(db)
    _visit(db, job, DAY1, tech="tech-1")
    _visit(db, job, DAY2, tech="tech-2", status="completed", arrived=DAY2)
    _visit(db, job, DAY4, tech="tech-2")
    db.commit()
    refusal = _refused(
        db, job, "double_booked", scheduled_at=DAY2,
        crew_before=["tech-1", "tech-2"], crew_after=("tech-2",),
    )
    assert refusal.detail["tech_ids"] == ["tech-2"]


def test_r2_a_time_change_within_n_is_not_refused(db):
    job = _job(db)
    _visit(db, job, DAY1)
    db.commit()
    assert _edit(db, job, scheduled_at=DAY1 + timedelta(hours=3)) is None


def test_r3_two_open_visits_for_one_tech_on_n_are_refused(db):
    job = _job(db)
    _visit(db, job, DAY1)
    _visit(db, job, DAY1 + timedelta(hours=4))
    db.commit()
    _refused(db, job, "two_open_visits", scheduled_at=DAY2)


@pytest.mark.parametrize("target", [DAY1 + timedelta(hours=1), DAY2, None])
def test_r4_a_crew_on_site_blocks_a_date_or_time_change_or_a_clear(db, target):
    job = _job(db)
    _visit(db, job, DAY1, arrived=DAY1)
    db.commit()
    _refused(db, job, "crew_on_site", scheduled_at=target)


def test_r4_counts_an_on_site_visit_on_a_later_day(db):
    job = _job(db)
    _visit(db, job, DAY1)  # un-tapped Monday stays N
    _visit(db, job, DAY2, tech="tech-2", arrived=DAY2)
    db.commit()
    _refused(db, job, "crew_on_site", scheduled_at=DAY1 + timedelta(hours=2))


# ── Crew change: C1–C7 ────────────────────────────────────────────────


def test_c1_the_first_tech_takes_the_unassigned_visit(db):
    job = _job(db, assigned_to=None)
    slot = _visit(db, job, DAY1, tech=None)
    db.commit()
    assert _edit(db, job, crew_before=[], crew_after=("tech-1",)) is None
    assert [(v.id, v.tech_id) for v in _live(db, job)] == [(slot.id, "tech-1")]
    assert _actions(db, job) == ["visit_reassigned"]


def test_c2_an_added_tech_gets_a_visit_on_n_at_the_jobs_start(db):
    job, _ = _three_day_job(db)
    assert _edit(db, job, crew_before=["tech-1"], crew_after=("tech-1", "tech-2")) is None
    assert ("tech-2", DAY1, "scheduled") in _layout(db, job)
    assert sum(1 for t, _, _ in _layout(db, job) if t == "tech-2") == 1


def test_c3_an_added_tech_with_a_visit_on_n_gets_nothing(db):
    job = _job(db)
    _visit(db, job, DAY1)
    _visit(db, job, DAY1, tech="tech-2", status="completed", arrived=DAY1)
    db.commit()
    assert _edit(db, job, crew_before=["tech-1"], crew_after=("tech-1", "tech-2")) is None
    assert len(_live(db, job)) == 2


def test_c4_an_added_tech_on_a_job_with_no_current_day_gets_nothing(db):
    job = _job(db, scheduled_at=None)
    db.commit()
    assert _edit(db, job, crew_before=["tech-1"], crew_after=("tech-1", "tech-2")) is None
    assert _live(db, job) == []


def test_c5_a_removed_tech_loses_only_the_current_day(db):
    job, _ = _three_day_job(db)
    _visit(db, job, DAY1, tech="tech-2")
    _visit(db, job, DAY2, tech="tech-2")
    db.commit()
    assert _edit(db, job, crew_before=["tech-1", "tech-2"], crew_after=("tech-1",)) is None
    assert [(t, s) for t, s, _ in _layout(db, job) if t == "tech-2"] == [("tech-2", DAY2)]


def test_c6_removing_the_last_tech_keeps_the_booking_unassigned(db):
    job = _job(db)
    only = _visit(db, job, DAY1)
    db.commit()
    assert _edit(db, job, crew_before=["tech-1"], crew_after=()) is None
    assert [(v.id, v.tech_id) for v in _live(db, job)] == [(only.id, None)]


def test_c6_then_c1_a_swap_hands_the_visit_over(db):
    job = _job(db)
    only = _visit(db, job, DAY1)
    db.commit()
    assert _edit(db, job, crew_before=["tech-1"], crew_after=("tech-2",)) is None
    assert [(v.id, v.tech_id) for v in _live(db, job)] == [(only.id, "tech-2")]
    assert _actions(db, job) == ["visit_reassigned", "visit_reassigned"]


def test_c7_an_arrived_tech_keeps_their_day_when_removed(db):
    job = _job(db)
    _visit(db, job, DAY1, arrived=DAY1)
    _visit(db, job, DAY1, tech="tech-2")
    db.commit()
    assert _edit(db, job, crew_before=["tech-1", "tech-2"], crew_after=("tech-2",)) is None
    assert ("tech-1", DAY1, "scheduled") in _layout(db, job)


def test_crew_and_date_in_one_save_book_the_new_crew_on_the_new_day(db):
    job = _job(db)
    _visit(db, job, DAY1)
    db.commit()
    assert _edit(
        db, job, scheduled_at=DAY2, crew_before=["tech-1"], crew_after=("tech-1", "tech-2"),
    ) is None
    assert sorted(_layout(db, job)) == [
        ("tech-1", DAY2, "scheduled"), ("tech-2", DAY2, "scheduled"),
    ]


def test_a_finished_jobs_date_and_crew_edit_moves_no_visit(db):
    job = _job(db, stage="completed")
    _visit(db, job, DAY1)
    db.commit()
    before = _layout(db, job)
    assert _edit(db, job, scheduled_at=DAY3, crew_before=["tech-1"], crew_after=("tech-2",)) is None
    assert _layout(db, job) == before


# ── Finished: F1 and X1 ───────────────────────────────────────────────


def test_f1_closes_the_worked_days_and_retires_the_rest(db):
    job = _job(db)
    tapped = _visit(db, job, DAY1, arrived=DAY1)
    untapped = _visit(db, job, DAY2, tech="tech-2")
    later = _visit(db, job, DAY4)
    db.commit()
    assert _edit(db, job, finish_day=shop_day(DAY3, shop_tz(db))) is None
    db.expire_all()
    assert db.get(Appointment, tapped.id).status == "completed"
    assert _utc(db.get(Appointment, tapped.id).arrived_at) == DAY1
    assert db.get(Appointment, untapped.id).status == "completed"
    assert db.get(Appointment, untapped.id).arrived_at is None  # none invented
    assert db.get(Appointment, later.id).deleted_at is not None
    assert sorted(_actions(db, job)) == ["visit_closed", "visit_closed", "visit_retired"]


def test_x1_retires_open_visits_and_closes_on_site_ones_as_cancelled(db):
    job = _job(db)
    on_site = _visit(db, job, DAY1, arrived=DAY1)
    booked = _visit(db, job, DAY2)
    db.commit()
    assert _edit(db, job, cancel=True) is None
    db.expire_all()
    kept = db.get(Appointment, on_site.id)
    assert kept.status == "cancelled" and _utc(kept.arrived_at) == DAY1
    assert db.get(Appointment, booked.id).deleted_at is not None


def test_x1_asks_about_an_old_status_only_arrival(db):
    job = _job(db)
    _visit(db, job, DAY1, status="arrived")
    _visit(db, job, DAY2)
    db.commit()
    refusal = _refused(db, job, "needs_answer", cancel=True)
    assert refusal.detail["question"] == "status_only_arrival"


# ── Re-open: R0 ───────────────────────────────────────────────────────


def _reopen(db, job, **kwargs):
    reopen_day = shop_day(_utc(job.completed_at) or datetime.now(UTC), shop_tz(db))
    return _edit(
        db, job, reopen=True, reopen_day=reopen_day,
        fields=job_visit_fields(db, job), **kwargs,
    )


def test_r0_closes_a_completed_jobs_leftover_visits_then_books_the_new_date(db):
    job = _job(db, stage="completed")
    job.completed_at = DAY2
    left = _visit(db, job, DAY1, arrived=DAY1)
    db.commit()
    job.lifecycle_stage = "in_progress"  # the route flips the stage after planning
    plan = plan_for_job(
        db, job, job_state="completed", reopen=True,
        reopen_day=shop_day(DAY2, shop_tz(db)), scheduled_at=DAY4,
        fields=job_visit_fields(db, job),
    )
    assert plan.refusal is None
    job.scheduled_at = plan.scheduled_at
    apply_visit_plan(db, job, plan, "user-1")
    recompute_job_schedule(db, job, "user-1", "job_reopened")
    db.commit()
    db.expire_all()
    assert db.get(Appointment, left.id).status == "completed"
    assert ("tech-1", DAY4, "scheduled") in _layout(db, job)
    assert _utc(db.get(Job, job.id).scheduled_at) == DAY4


@pytest.mark.parametrize("answer, booked", [(None, None), (True, 2), (False, 1)])
def test_r0_asks_before_rebooking_a_closed_day(db, answer, booked):
    job = _job(db, stage="completed")
    job.completed_at = DAY1
    _visit(db, job, DAY1, status="completed", arrived=DAY1)
    db.commit()
    result = _reopen(db, job, scheduled_at=DAY1 + timedelta(hours=1), rebook_closed_day=answer)
    if booked is None:
        assert result is not None and result.code == "needs_answer"
        assert result.detail["question"] == "rebook_closed_day"
        assert result.detail["tech_ids"] == ["tech-1"]
        assert len(_live(db, job)) == 1
    else:
        assert result is None
        assert len(_live(db, job)) == booked


# ── Recompute: invariant I ────────────────────────────────────────────


def test_recompute_puts_the_job_on_its_earliest_current_visit(db):
    job = _job(db, scheduled_at=DAY4)
    _visit(db, job, DAY1, status="completed", arrived=DAY1)
    _visit(db, job, DAY2 + timedelta(seconds=7, microseconds=9))
    db.commit()
    recompute_job_schedule(db, job, "user-1", "test")
    db.commit()
    assert _utc(job.scheduled_at) == DAY2 + timedelta(seconds=7, microseconds=9)  # exact, not truncated
    row = db.execute(
        select(AuditLog).where(AuditLog.action == "job_schedule_recomputed")
    ).scalar_one()
    assert row.details["reason"] == "test"


def test_recompute_leaves_a_finished_job_alone(db):
    job = _job(db, scheduled_at=DAY4, stage="cancelled")
    _visit(db, job, DAY1)
    db.commit()
    recompute_job_schedule(db, job, "user-1", "test")
    assert _utc(job.scheduled_at) == DAY4


def test_job_crew_reads_assignments_in_the_order_they_were_made(db):
    from gdx_dispatch.models.tenant_models import JobAssignment

    job = _job(db)
    # Ids and tech ids both sort the other way, so only assigned_at can
    # produce this order.
    for row_id, tech, minutes in (("b", "tech-z", 0), ("a", "tech-y", 5)):
        db.add(JobAssignment(id=row_id * 36, job_id=str(job.id), tech_id=tech,
                             is_lead=False, assigned_at=DAY1 + timedelta(minutes=minutes)))
    db.commit()
    assert job_crew(db, db.get(Job, job.id)) == ["tech-z", "tech-y"]


# ── Arrival: A1–A3 ────────────────────────────────────────────────────


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
    tap = db.execute(
        select(AuditLog).where(AuditLog.action == "arrived", AuditLog.entity_type == "job")
    ).scalar_one()
    assert tap.details["visit_id"] == str(mine.id)
    assert tap.details["tech_id"] == TECH


def test_arrival_with_a_visit_on_another_day_stamps_today(app_and_db):  # noqa: F811
    client, db = app_and_db
    job, mine, _, old = _seed_arrival(db, yesterday=True)
    r = client.post(f"/api/mobile/jobs/{job.id.hex}/arrived")
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(Appointment, mine.id).arrived_at is not None
    assert db.get(Appointment, old.id).arrived_at is None


def _arrive(db, job, when, tech="tech-1"):
    """What the arrival route does with the visit it picks."""
    visit = mobile_router._arrival_visit(db, job.id, tech, when)
    moved = mobile_router._stamp_arrival(db, visit, when) if visit is not None else None
    recompute_job_schedule(db, job, "user-1", "arrival")
    db.commit()
    return visit, moved


def test_a1_prefers_this_techs_own_visit_over_an_unassigned_one(db):
    job = _job(db)
    _visit(db, job, DAY1, tech=None)
    mine = _visit(db, job, DAY1)
    db.commit()
    assert mobile_router._arrival_visit(db, job.id, "tech-1", DAY1).id == mine.id


def test_a1_takes_a_same_day_return_over_the_worked_visit(db):
    job = _job(db)
    _visit(db, job, DAY1, arrived=DAY1)
    ret = _visit(db, job, DAY1 + timedelta(hours=5))
    db.commit()
    picked = mobile_router._arrival_visit(db, job.id, "tech-1", DAY1 + timedelta(hours=5))
    assert picked.id == ret.id


def test_a1_an_arrival_on_the_visit_day_moves_nothing(db):
    job = _job(db)
    only = _visit(db, job, DAY1)
    db.commit()
    _, moved = _arrive(db, job, DAY1 + timedelta(minutes=20))
    assert moved is None and _utc(only.start_at) == DAY1 and _utc(job.scheduled_at) == DAY1


def test_a2_an_early_arrival_moves_the_only_visit_and_recompute_moves_the_job(db):
    job = _job(db, scheduled_at=DAY3)
    only = _visit(db, job, DAY3)
    db.commit()
    arrived = DAY1 + timedelta(hours=1, seconds=12)
    visit, moved = _arrive(db, job, arrived)
    assert visit.id == only.id
    assert _utc(only.start_at) == DAY1 + timedelta(hours=1)  # to the minute
    assert _utc(job.scheduled_at) == _utc(only.start_at)  # I, by recompute
    # What the arrival's audit row records, so an undo can put it back.
    assert moved["visit_id"] == str(only.id)
    assert _utc(datetime.fromisoformat(moved["visit_start_from"])) == DAY3
    assert _utc(datetime.fromisoformat(moved["visit_end_from"])) == DAY3 + timedelta(hours=8)


def test_a2_never_takes_another_techs_visit(db):
    # Doug, Q3: an A2 visit is this tech's or unassigned.
    job = _job(db)
    _visit(db, job, DAY1, tech="tech-2")
    db.commit()
    assert mobile_router._arrival_visit(db, job.id, "tech-1", DAY1 + timedelta(days=1)) is None


def test_a2_takes_an_unassigned_visit_on_another_day(db):
    job = _job(db)
    slot = _visit(db, job, DAY3, tech=None)
    db.commit()
    assert mobile_router._arrival_visit(db, job.id, "tech-1", DAY1).id == slot.id


def test_a2_ignores_closed_and_cancelled_rows_when_counting(db):
    # #846 required the job's only row of any status; rev 2 counts Current.
    job = _job(db)
    _visit(db, job, DAY1 - timedelta(days=1), status="cancelled")
    only = _visit(db, job, DAY3)
    db.commit()
    assert mobile_router._arrival_visit(db, job.id, "tech-1", DAY1).id == only.id


def test_a2_a_retap_after_the_office_closed_today_pulls_nothing(db):
    job = _job(db)
    _visit(db, job, DAY1, status="completed", arrived=DAY1)
    _visit(db, job, DAY3)
    db.commit()
    assert mobile_router._arrival_visit(db, job.id, "tech-1", DAY1 + timedelta(hours=2)) is None


def test_a3_a_multi_visit_job_with_none_today_stamps_nothing(db):
    job = _job(db)
    _visit(db, job, DAY1 - timedelta(days=1))
    _visit(db, job, DAY3)
    db.commit()
    assert mobile_router._arrival_visit(db, job.id, "tech-1", DAY2) is None


def test_a3_never_moves_a_status_only_arrived_visit(db):
    # ON SITE, so it is no OPEN visit A2 may take; a later-day tap stamps nothing.
    job = _job(db)
    marked = _visit(db, job, DAY1, status="arrived")
    db.commit()
    visit, moved = _arrive(db, job, DAY3 + timedelta(hours=1))
    assert visit is None and moved is None
    assert _utc(marked.start_at) == DAY1 and _utc(job.scheduled_at) == DAY1


def test_a3_skips_other_techs_when_the_job_has_several(db):
    job = _job(db)
    _visit(db, job, DAY1, tech="tech-2")
    _visit(db, job, DAY1, tech="tech-3")
    db.commit()
    assert mobile_router._arrival_visit(db, job.id, "tech-1", DAY1) is None


def test_a_tap_on_n_leaves_the_date_on_n(db):
    job = _job(db)
    _visit(db, job, DAY1)
    _visit(db, job, DAY2)
    db.commit()
    _arrive(db, job, DAY1 + timedelta(hours=1))
    assert _utc(job.scheduled_at) == DAY1
