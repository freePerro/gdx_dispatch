"""A finished job sits on its completion day and holds no open visit.

2026-10-15: 16 completed jobs with no scheduled date came back from
``/api/jobs?date=`` for every date, and the dispatch boards drew each in its
tech's column on every day; 36 visits left open on completed jobs (from before
completion closed visits, #868) drew them again as open work. Two server
halves are pinned here:

* the date scope: an undated job is returned while it is live, and once it is
  completed only for a window that holds its completion; an undated cancelled
  job is never returned;
* the appointments router, which writes visits beside the job's own visit
  routes: it refuses to book a visit on a finished job, to move one onto it,
  or to reopen one there (409 ``job_finished``, the code the job's visit
  routes answer), and still lets an already-open legacy visit be edited and
  closed.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401 — registers tables
from gdx_dispatch.models.tenant_models import Appointment, Job

TENANT = "tenant-test"
DAY = datetime(2026, 7, 24, tzinfo=UTC)


@pytest.fixture()
def harness():
    from gdx_dispatch.core.auth import get_current_user as core_gcu
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.routers import appointments, jobs
    from gdx_dispatch.routers.auth import get_current_user as routers_gcu

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    app = FastAPI()

    @app.middleware("http")
    async def _tenant_shim(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": TENANT}
        return await call_next(request)

    app.include_router(jobs.router)
    app.include_router(appointments.router)

    def _db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    user = {"sub": "u-1", "user_id": "u-1", "role": "admin", "tenant_id": TENANT}
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[routers_gcu] = lambda: user
    app.dependency_overrides[core_gcu] = lambda: user
    try:
        yield TestClient(app), SessionLocal
    finally:
        engine.dispose()


def _job(SessionLocal, title, stage, *, scheduled_at=None, completed_at=None, status="Scheduled") -> UUID:
    with SessionLocal() as db:
        j = Job(
            id=uuid4(), company_id=TENANT, title=title, status=status, lifecycle_stage=stage,
            scheduled_at=scheduled_at, completed_at=completed_at, assigned_to="tech-1",
        )
        db.add(j)
        db.commit()
        return j.id


def _visit(SessionLocal, job_id, status="scheduled") -> UUID:
    start = DAY + timedelta(hours=15)
    with SessionLocal() as db:
        a = Appointment(
            company_id=TENANT, job_id=job_id, title="visit", tech_id="tech-1",
            start_at=start, end_at=start + timedelta(hours=1), status=status,
        )
        db.add(a)
        db.commit()
        return a.id


# ── the date scope ─────────────────────────────────────────────────────────


def test_an_undated_finished_job_comes_back_only_for_its_completion_window(harness):
    client, SessionLocal = harness
    _job(SessionLocal, "undated-live", "service_call", status="New")
    _job(SessionLocal, "undated-done-today", "completed", completed_at=DAY + timedelta(hours=18), status="Completed")
    _job(SessionLocal, "undated-done-earlier", "completed", completed_at=DAY - timedelta(days=14), status="Completed")
    _job(SessionLocal, "undated-done-no-time", "completed", status="Completed")
    _job(SessionLocal, "undated-cancelled", "cancelled", status="Cancelled")
    _job(SessionLocal, "dated-done", "completed", scheduled_at=DAY + timedelta(hours=9),
         completed_at=DAY + timedelta(hours=11), status="Completed")

    def titles(query):
        r = client.get(f"/api/jobs?{query}")
        assert r.status_code == 200, r.text
        return {j["title"] for j in r.json()["items"]}

    on_day = titles("date=2026-07-24")
    assert {"undated-live", "undated-done-today", "dated-done"} <= on_day
    assert not {"undated-done-earlier", "undated-done-no-time", "undated-cancelled"} & on_day

    weeks_later = titles("date=2026-08-20")
    assert "undated-live" in weeks_later, "live intake still shows on any day"
    assert not {"undated-done-today", "undated-done-earlier", "undated-done-no-time",
                "undated-cancelled", "dated-done"} & weeks_later

    week = titles("date_from=2026-07-05&date_to=2026-07-11")
    assert "undated-done-earlier" in week
    assert "undated-done-today" not in week


# ── the appointments router ────────────────────────────────────────────────


def _payload(job_id, **over) -> dict:
    start = DAY + timedelta(days=1, hours=15)
    body = {
        "title": "Visit", "tech_id": "tech-1", "job_id": str(job_id),
        "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat(),
    }
    body.update(over)
    return body


@pytest.mark.parametrize("stage", ["completed", "cancelled"])
def test_no_visit_is_booked_on_a_finished_job(harness, stage):
    client, SessionLocal = harness
    job_id = _job(SessionLocal, "finished", stage, completed_at=DAY if stage == "completed" else None)

    r = client.post("/api/appointments", json=_payload(job_id))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "job_finished"
    assert stage in r.json()["detail"]
    with SessionLocal() as db:
        assert db.query(Appointment).count() == 0


def test_a_visit_is_still_booked_on_a_live_job(harness):
    client, SessionLocal = harness
    job_id = _job(SessionLocal, "live", "scheduled")
    r = client.post("/api/appointments", json=_payload(job_id))
    assert r.status_code == 201, r.text


def test_an_open_visit_cannot_be_moved_onto_a_finished_job(harness):
    client, SessionLocal = harness
    live = _job(SessionLocal, "live", "scheduled")
    done = _job(SessionLocal, "done", "completed", completed_at=DAY)
    visit = _visit(SessionLocal, live)

    r = client.patch(f"/api/appointments/{visit}", json={"job_id": str(done)})
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "job_finished"
    with SessionLocal() as db:
        assert db.get(Appointment, visit).job_id == live


def test_a_closed_visit_on_a_finished_job_cannot_be_reopened(harness):
    client, SessionLocal = harness
    done = _job(SessionLocal, "done", "completed", completed_at=DAY)
    visit = _visit(SessionLocal, done, status="completed")

    r = client.patch(f"/api/appointments/{visit}", json={"status": "scheduled"})
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "job_finished"
    # A note on the closed visit is still fine: it stays closed.
    r = client.patch(f"/api/appointments/{visit}", json={"notes": "gate code 1234"})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        assert db.get(Appointment, visit).status == "completed"


def test_a_legacy_open_visit_on_a_finished_job_can_still_be_edited_and_closed(harness):
    client, SessionLocal = harness
    done = _job(SessionLocal, "done", "completed", completed_at=DAY)
    visit = _visit(SessionLocal, done, status="confirmed")

    r = client.patch(f"/api/appointments/{visit}", json={"notes": "left before F1"})
    assert r.status_code == 200, r.text
    r = client.patch(f"/api/appointments/{visit}", json={"status": "completed"})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        assert db.get(Appointment, visit).status == "completed"
