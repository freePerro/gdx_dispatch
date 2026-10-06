"""Job → Appointment auto-sync.

A job created via /api/jobs with scheduled_at must mirror to the
appointments table so the Appointments page (and unconfirmed-arrivals
list) sees it. Discovered 2026-05-06 — Doug created an "89 Lumber" job
that surfaced in /jobs but never appeared on /appointments because
nothing kept the two records in sync.

Since the multi-day jobs plan §5.2a (PR 1b) the sync is a planner
(``plan_for_job``) and an applier (``apply_visit_plan``), with
``recompute_job_schedule`` last; these drive them the way the job routes do.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models.tenant_models  # noqa: F401
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Appointment, Job, JobAssignment
from gdx_dispatch.routers.jobs import _set_job_assignments
from gdx_dispatch.services.visit_sync import (
    UNSET,
    apply_visit_plan,
    job_visit_fields,
    plan_for_job,
    recompute_job_schedule,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sess = Session()
    yield sess
    sess.close()
    engine.dispose()


def _make_job(scheduled_at: datetime | None, assigned_to: str | None = "tech-1") -> Job:
    return Job(
        id=uuid.uuid4(),
        title="Install 10x8",
        company_id="tenant-test",
        scheduled_at=scheduled_at,
        status="Scheduled" if scheduled_at else "Lead",
        priority="Normal",
        job_type="Service",
        lifecycle_stage="scheduled" if scheduled_at else "lead",
        assigned_to=assigned_to,
        dispatch_status="assigned" if assigned_to else "unassigned",
        billing_status="unbilled",
        is_demo=False,
        is_return_visit=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _sync(db, job, *, scheduled_at=UNSET, crew_before=None, crew_after=None, fields=None, **kw):
    """Plan and apply one edit, as the job routes do, then recompute."""
    plan = plan_for_job(
        db, job, crew_before=crew_before, scheduled_at=scheduled_at,
        crew_after=crew_after, fields=fields, **kw,
    )
    assert plan.refusal is None, plan.refusal
    if plan.scheduled_at is not UNSET:
        job.scheduled_at = plan.scheduled_at
    apply_visit_plan(db, job, plan, "user-1")
    recompute_job_schedule(db, job, "user-1", "test")
    db.commit()


def _create(db, job, start):
    """create_job: a new job has no stored date, so its date is a date set (E4)."""
    _sync(db, job, scheduled_at=start, stored_scheduled_at=None, fields=job_visit_fields(db, job))


def _active(db, job):
    return db.execute(
        select(Appointment).where(
            Appointment.job_id == job.id,
            Appointment.deleted_at.is_(None),
        ).order_by(Appointment.tech_id)
    ).scalars().all()


def _minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def test_sync_creates_appointment_for_scheduled_job(db):
    start = _minute(datetime.now(timezone.utc) + timedelta(hours=2))
    job = _make_job(scheduled_at=start)
    db.add(job)
    db.flush()

    # create_job: E4 books the crew on the new date.
    _create(db, job, start)

    appt = db.execute(select(Appointment).where(Appointment.job_id == job.id)).scalar_one()
    assert appt.title == "Install 10x8"
    assert appt.tech_id == "tech-1"
    # SQLite drops tzinfo on round-trip; compare wall clock.
    assert appt.start_at.replace(tzinfo=timezone.utc) == start
    assert appt.end_at.replace(tzinfo=timezone.utc) == start + timedelta(minutes=60)
    assert appt.status == "scheduled"
    assert appt.company_id == "tenant-test"


def test_a_resave_books_nothing_new_and_a_move_moves_the_visit(db):
    start = _minute(datetime.now(timezone.utc) + timedelta(hours=2))
    job = _make_job(scheduled_at=start)
    db.add(job)
    db.flush()
    _create(db, job, start)

    # E1: the same date and a new title copy the title, insert nothing.
    job.title = "Install 10x8 — renamed"
    _sync(db, job, scheduled_at=start, fields=job_visit_fields(db, job))
    assert [a.title for a in _active(db, job)] == ["Install 10x8 — renamed"]

    # E2: a time change moves the visit; no duplicate.
    new_start = start + timedelta(hours=3)
    _sync(db, job, scheduled_at=new_start)
    active = _active(db, job)
    assert len(active) == 1
    assert active[0].start_at.replace(tzinfo=timezone.utc) == new_start
    assert job.scheduled_at.replace(tzinfo=timezone.utc) == new_start


def test_sync_fans_out_one_appointment_per_assigned_tech(db):
    start = _minute(datetime.now(timezone.utc) + timedelta(hours=4))
    job = _make_job(scheduled_at=start, assigned_to="tech-1")
    db.add(job)
    db.flush()

    _set_job_assignments(
        db, job_id=str(job.id), tech_ids=["tech-1", "tech-2"],
        lead_tech_id="tech-1", user_id="user-1",
    )
    job = db.get(Job, job.id)
    _create(db, job, start)

    active = _active(db, job)
    assert {a.tech_id for a in active} == {"tech-1", "tech-2"}
    assert all(a.start_at.replace(tzinfo=timezone.utc) == start for a in active)

    # Drop tech-2 (C5): their open visit retires; tech-1's stays.
    _set_job_assignments(
        db, job_id=str(job.id), tech_ids=["tech-1"],
        lead_tech_id=None, user_id="user-1",
    )
    job = db.get(Job, job.id)
    _sync(db, job, crew_before=["tech-1", "tech-2"], crew_after=("tech-1",))
    assert [a.tech_id for a in _active(db, job)] == ["tech-1"]


def test_set_job_assignments_lead_falls_back_to_first(db):
    job = _make_job(scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1))
    db.add(job)
    db.flush()

    _set_job_assignments(
        db, job_id=str(job.id), tech_ids=["tech-a", "tech-b"],
        lead_tech_id=None, user_id="user-1",
    )
    db.commit()
    rows = db.execute(
        select(JobAssignment).where(
            JobAssignment.job_id == str(job.id),
            JobAssignment.deleted_at.is_(None),
        )
    ).scalars().all()
    leads = [r.tech_id for r in rows if r.is_lead]
    assert leads == ["tech-a"]
    # Verify via fresh SELECT — the helper writes via raw SQL so the ORM
    # identity map needs to round-trip; assert from the DB directly.
    primary = db.execute(
        select(Job.assigned_to).where(Job.id == job.id)
    ).scalar()
    assert primary == "tech-a"


def test_sync_skips_unscheduled_jobs(db):
    job = _make_job(scheduled_at=None, assigned_to=None)
    db.add(job)
    db.flush()

    _sync(db, job, fields=job_visit_fields(db, job))

    appts = db.execute(select(Appointment).where(Appointment.job_id == job.id)).scalars().all()
    assert appts == []
