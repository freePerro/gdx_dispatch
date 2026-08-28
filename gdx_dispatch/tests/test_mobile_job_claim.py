"""2026-08-28 field report — a tech could not add photos to the job he made.

He created the customer and job from the mobile dialog with "Assign to me"
switched off. The job landed unassigned, the detail screen came back
creator-grant (read-only), and the Add-photo control was hidden with the
rest of the action bar. Two things pinned here:

1. ``can_add_photos`` — a creator-grant view keeps the ONE write that is
   not hours evidence. Company-wide browsing does not get it.
2. ``POST /api/mobile/jobs/{id}/claim`` — the create-time toggle, one
   screen later: puts the CALLER's technician record on the job, only
   while it is still the caller's own unassigned job.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Customer, Job, JobAssignment, Technician
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"
USER = "user-1"
TECH = "tech-1"
OTHER_USER = "user-2"
OTHER_TECH = "tech-2"


def _build_app(db, *, user_id: str = USER, role: str = "technician") -> TestClient:
    from gdx_dispatch.core.modules import require_module

    app = FastAPI()
    app.include_router(mobile_router.router)
    user = {"user_id": user_id, "tenant_id": TENANT, "role": role}
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


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def _seed(db, *, created_by=USER, assigned_to=None, with_tech=True) -> Job:
    if with_tech:
        db.add(Technician(id=TECH, company_id=TENANT, user_id=USER, active=True))
    db.add(Technician(id=OTHER_TECH, company_id=TENANT, user_id=OTHER_USER, active=True))
    c = Customer(id=uuid4(), name="Acme", phone="555-1111", company_id=TENANT)
    db.add(c)
    db.commit()
    j = Job(
        id=uuid4(), company_id=TENANT, customer_id=c.id, title="Install new doors 9x7",
        description="d", created_by=created_by, assigned_to=assigned_to,
        dispatch_status="assigned" if assigned_to else "unassigned",
        scheduled_at=datetime.now(UTC),
    )
    db.add(j)
    db.commit()
    return j


def _detail(client, job):
    r = client.get(f"/api/mobile/job/{job.id.hex}")
    assert r.status_code == 200, r.text
    return r.json()


# ── can_add_photos ───────────────────────────────────────────────────


def test_creator_grant_is_read_only_but_may_add_photos(db):
    j = _seed(db)
    body = _detail(_build_app(db), j)
    assert body["access_grant"] == "creator"
    assert body["read_only"] is True
    assert body["can_add_photos"] is True


def test_assigned_tech_may_add_photos(db):
    j = _seed(db, assigned_to=TECH)
    body = _detail(_build_app(db), j)
    assert body["access_grant"] == "assigned"
    assert body["can_add_photos"] is True


def test_company_grant_may_not_add_photos(db, monkeypatch):
    # Someone else's job, visible only through techs_see_all_jobs.
    j = _seed(db, created_by=OTHER_USER, assigned_to=OTHER_TECH)
    monkeypatch.setattr(mobile_router, "_company_jobs_scope_allowed", lambda *a, **k: True)
    body = _detail(_build_app(db), j)
    assert body["access_grant"] == "company"
    assert body["read_only"] is True
    assert body["can_add_photos"] is False


# ── claim ────────────────────────────────────────────────────────────


def test_creator_claims_own_unassigned_job(db):
    j = _seed(db)
    client = _build_app(db)
    r = client.post(f"/api/mobile/jobs/{j.id.hex}/claim")
    assert r.status_code == 200, r.text
    assert r.json()["assigned_to"] == TECH

    db.expire_all()
    row = db.get(Job, j.id)
    assert row.assigned_to == TECH
    assert row.dispatch_status == "assigned"
    links = db.execute(
        select(JobAssignment).where(JobAssignment.deleted_at.is_(None))
    ).scalars().all()
    assert [(a.tech_id, a.is_lead) for a in links] == [(TECH, True)]

    # The grant flipped: the next read is the full, writable screen.
    body = _detail(client, j)
    assert body["access_grant"] == "assigned"
    assert body["read_only"] is False

    # Who did it, what changed, when.
    mine = db.execute(
        select(AuditLog).where(AuditLog.action == "job_self_assigned")
    ).scalars().all()
    assert len(mine) == 1
    assert mine[0].user_id == USER
    assert mine[0].details["assigned_to"] == TECH


def test_claim_is_idempotent_once_assigned(db):
    j = _seed(db)
    client = _build_app(db)
    assert client.post(f"/api/mobile/jobs/{j.id.hex}/claim").status_code == 200
    assert client.post(f"/api/mobile/jobs/{j.id.hex}/claim").status_code == 200
    links = db.execute(
        select(JobAssignment).where(JobAssignment.deleted_at.is_(None))
    ).scalars().all()
    assert len(links) == 1


def test_claim_refused_once_dispatch_assigned_elsewhere(db):
    # The creator's own job, but dispatch already handed it to another tech:
    # the tap must not steal it back — 409 with a reason the screen can show.
    j = _seed(db, assigned_to=OTHER_TECH)
    r = _build_app(db).post(f"/api/mobile/jobs/{j.id.hex}/claim")
    assert r.status_code == 409, r.text
    db.expire_all()
    assert db.get(Job, j.id).assigned_to == OTHER_TECH


def test_claim_on_someone_elses_job_is_opaque_404(db, monkeypatch):
    # Even with company-wide browsing switched on: reading is not claiming.
    j = _seed(db, created_by=OTHER_USER)
    monkeypatch.setattr(mobile_router, "_company_jobs_scope_allowed", lambda *a, **k: True)
    r = _build_app(db).post(f"/api/mobile/jobs/{j.id.hex}/claim")
    assert r.status_code == 404, r.text
    db.expire_all()
    assert db.get(Job, j.id).assigned_to is None


def test_claim_without_technician_record_is_409_not_a_poisoned_row(db):
    # An office account that created a job from the phone: nothing to assign.
    j = _seed(db, with_tech=False)
    r = _build_app(db).post(f"/api/mobile/jobs/{j.id.hex}/claim")
    assert r.status_code == 409, r.text
    db.expire_all()
    assert db.get(Job, j.id).assigned_to is None
    assert db.execute(select(JobAssignment)).scalars().all() == []
