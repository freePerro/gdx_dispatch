"""GDXA-378 — the lazy assignment back-fill writes crew only for the assignee.

``ensure_assignment_for_legacy_job`` back-fills a ``JobAssignment`` for a
single-tech-era job so the per-tech mobile stamps land somewhere. It wrote a
row for whoever tapped, without checking ``Job.assigned_to``; the mobile
gate (``_assert_job_access``) admits any dispatch manager, so a dispatcher
who also has a technician record and tapped On my way / I'm here / Complete
on another tech's job became that job's sole crew row. The first row
replaces the ``assigned_to`` fallback in ``job_crew``, so it now back-fills
only for the lead: a job with no live crew row whose ``assigned_to`` is the
caller, or is NULL with appointments naming the caller and nobody else.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import (
    Appointment,
    Customer,
    Job,
    JobAssignment,
    Technician,
)
from gdx_dispatch.routers import job_assignments as ja
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"
USER = "user-1"
TECH = "tech-1"
OTHER_USER = "user-2"
OTHER_TECH = "tech-2"


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def _client(db, role: str) -> TestClient:
    from gdx_dispatch.core.modules import require_module

    app = FastAPI()
    app.include_router(mobile_router.router)
    user = {"user_id": USER, "tenant_id": TENANT, "role": role}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_module("mobile")] = lambda: True

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "test"}
        request.state.tenant_id = TENANT
        request.state.user = user
        return await call_next(request)

    return TestClient(app)


def _seed(db, *, assigned_to: str | None) -> Job:
    now = datetime.now(UTC)
    db.add(Technician(id=TECH, company_id=TENANT, user_id=USER, active=True, created_at=now))
    db.add(Technician(id=OTHER_TECH, company_id=TENANT, user_id=OTHER_USER, active=True,
                      created_at=now))
    c = Customer(id=uuid4(), name="Acme", phone="555-1111", company_id=TENANT)
    db.add(c)
    db.commit()
    d = now.date()
    j = Job(
        id=uuid4(), company_id=TENANT, customer_id=c.id, title="Spring",
        description="d", scheduled_at=datetime(d.year, d.month, d.day, 12, tzinfo=UTC),
        assigned_to=assigned_to, dispatch_status="assigned", lifecycle_stage="scheduled",
    )
    db.add(j)
    db.commit()
    return j


@pytest.mark.parametrize(("path", "body"), [
    ("en-route", {}),
    ("arrived", None),
    ("complete", {"signature_data": "data:image/png;base64,iVBORw0KGgo=", "signed_by": "Pat"}),
])
def test_manager_technician_tap_on_another_techs_job_writes_no_crew(db, path, body):
    """The defect: a dispatcher with a technician record taps a state button
    on a job assigned_to another tech. The tap is allowed (manager gate), but
    the caller must not become the job's crew."""
    job = _seed(db, assigned_to=OTHER_TECH)
    r = _client(db, "dispatcher").post(f"/api/mobile/jobs/{job.id.hex}/{path}", json=body)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.query(JobAssignment).count() == 0
    assert db.get(Job, job.id).assigned_to == OTHER_TECH


def test_assignee_tap_still_backfills_and_stamps(db):
    """Control: the same request from the assigned tech creates the row and
    stamps it — the refusal above is the assignee check, nothing else."""
    job = _seed(db, assigned_to=TECH)
    r = _client(db, "technician").post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={})
    assert r.status_code == 200, r.text
    row = db.query(JobAssignment).one()
    assert row.tech_id == TECH
    assert row.assigned_by == "system_lazy_backfill"
    assert row.en_route_at is not None


def test_legacy_assigned_to_holding_the_users_id_still_backfills(db):
    """job_belongs_to_user accepts assigned_to == the caller's users.id on
    legacy rows; the back-fill honours the same form."""
    job = _seed(db, assigned_to=USER)
    row = ja.ensure_assignment_for_legacy_job(db, job_id=job.id.hex, tech_id=TECH, user_id=USER)
    assert row is not None and row.tech_id == TECH


def test_recreated_technician_record_still_backfills(db):
    """A tech whose technician row was recreated: assigned_to holds the old
    row, the resolver hands the new one. job_belongs_to_user owns it via
    technicians.user_id, so the stamp must land (audit finding, GDXA-378)."""
    job = _seed(db, assigned_to=None)
    db.add(Technician(id="tech-old", company_id=TENANT, user_id=USER, active=False,
                      created_at=datetime.now(UTC) - timedelta(days=30)))
    db.get(Job, job.id).assigned_to = "tech-old"
    db.commit()
    r = _client(db, "technician").post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={})
    assert r.status_code == 200, r.text
    row = db.query(JobAssignment).one()
    assert row.tech_id == TECH and row.en_route_at is not None


def _appointment(db, job, tech_id):
    now = datetime.now(UTC)
    db.add(Appointment(id=uuid4(), company_id=TENANT, job_id=job.id, tech_id=tech_id,
                       title="t", start_at=now, end_at=now + timedelta(hours=2)))
    db.commit()


def test_appointment_helper_on_a_legacy_job_does_not_displace_the_lead(db):
    """Pre-Phase-1.4 a second tech rode along through an Appointment while
    assigned_to named the lead. A row for the helper alone would become the
    whole crew (job_crew drops the assigned_to fallback once any row exists),
    so the helper gets no row (audit, GDXA-378)."""
    job = _seed(db, assigned_to=OTHER_TECH)
    _appointment(db, job, TECH)
    r = _client(db, "technician").post(f"/api/mobile/jobs/{job.id.hex}/arrived")
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.query(JobAssignment).count() == 0


def test_appointment_helper_joins_once_the_lead_has_a_row(db):
    """Lead taps first, then the helper: the helper's row displaces nobody,
    so it lands and the crew is both (third audit, GDXA-378)."""
    job = _seed(db, assigned_to=OTHER_TECH)
    _appointment(db, job, OTHER_TECH)
    _appointment(db, job, TECH)
    lead = ja.ensure_assignment_for_legacy_job(db, job_id=job.id.hex, tech_id=OTHER_TECH,
                                               user_id=OTHER_USER)
    helper = ja.ensure_assignment_for_legacy_job(db, job_id=job.id.hex, tech_id=TECH, user_id=USER)
    assert lead is not None and helper is not None
    assert sorted(r.tech_id for r in db.query(JobAssignment).all()) == [TECH, OTHER_TECH]


def test_unassigned_job_with_another_techs_appointment_is_not_joined(db):
    """assigned_to NULL but an appointment names tech-2: the job is not
    unowned, and a manager-technician's tap must not make them its crew."""
    job = _seed(db, assigned_to=None)
    _appointment(db, job, OTHER_TECH)
    r = _client(db, "dispatcher").post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.query(JobAssignment).count() == 0


def test_unassigned_job_with_the_callers_own_appointment_backfills(db):
    job = _seed(db, assigned_to=None)
    _appointment(db, job, TECH)
    row = ja.ensure_assignment_for_legacy_job(db, job_id=str(job.id), tech_id=TECH, user_id=USER)
    assert row is not None and row.tech_id == TECH


def test_crew_row_in_the_other_id_form_still_counts_as_crew(db):
    """A dashed crew row and a tap through the hex URL: the job has crew, so
    the caller is not added beside it (audit finding 3, GDXA-378)."""
    job = _seed(db, assigned_to=None)
    db.add(JobAssignment(id=str(uuid4()), job_id=str(job.id), tech_id=OTHER_TECH,
                         is_lead=True, assigned_at=datetime.now(UTC), assigned_by="disp"))
    db.commit()
    row = ja.ensure_assignment_for_legacy_job(db, job_id=job.id.hex, tech_id=TECH, user_id=USER)
    assert row is None
    assert [r.tech_id for r in db.query(JobAssignment).all()] == [OTHER_TECH]


def test_unassigned_job_without_appointment_is_not_joined_by_a_manager(db):
    """assigned_to NULL, no appointment, no crew: a technician 404s at the
    gate, so the only caller who reaches the back-fill is a manager, and it
    must not make them the job's crew (second audit, GDXA-378)."""
    job = _seed(db, assigned_to=None)
    r = _client(db, "dispatcher").post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.query(JobAssignment).count() == 0


def test_existing_crew_without_the_caller_is_not_joined(db):
    """A job with live crew rows is not a legacy job: the caller is not added
    to it even when assigned_to is NULL."""
    job = _seed(db, assigned_to=None)
    db.add(JobAssignment(id=str(uuid4()), job_id=job.id.hex, tech_id=OTHER_TECH,
                         is_lead=False, assigned_at=datetime.now(UTC), assigned_by="disp"))
    db.commit()
    row = ja.ensure_assignment_for_legacy_job(db, job_id=job.id.hex, tech_id=TECH, user_id=USER)
    assert row is None
    assert [r.tech_id for r in db.query(JobAssignment).all()] == [OTHER_TECH]


def test_existing_row_for_the_caller_is_returned(db):
    job = _seed(db, assigned_to=OTHER_TECH)
    rid = str(uuid4())
    db.add(JobAssignment(id=rid, job_id=job.id.hex, tech_id=TECH, is_lead=False,
                         assigned_at=datetime.now(UTC), assigned_by="disp"))
    db.commit()
    row = ja.ensure_assignment_for_legacy_job(db, job_id=job.id.hex, tech_id=TECH, user_id=USER)
    assert row is not None and row.id == rid
