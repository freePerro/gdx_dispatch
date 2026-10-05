"""GDXA-208 — two mobile defects found in the GDXA-207 triage.

1. The "is this a valid tech" test was written two ways. The ownership gate
   (core/job_access.job_belongs_to_user) matched ``technicians.user_id`` with
   no active filter; routers/mobile._get_technician_id resolved active rows
   only. A deactivated tech therefore passed the gate and then the per-tech
   arrived/complete/en-route stamps silently skipped. One rule now on the
   mobile routers (mobile, mobile_invoicing, mobile_quoting): a deactivated
   tech gets a 403 with the reason. core/job_access is unchanged, so office
   routes outside /api/mobile still match a deactivated row on ownership.

2. Cancel moves ``lifecycle_stage`` only and keeps ``scheduled_at`` and the
   appointment row, so a cancelled job dated today stayed on the tech's Today
   route and could become tomorrow's first stop on the day summary.
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
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers import mobile_day_summary as day_summary_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"
USER = "user-1"
TECH = "tech-1"


def _noon_today() -> datetime:
    # Noon UTC keeps +/- a few hours inside one UTC day (see test_mobile_today).
    d = datetime.now(UTC).date()
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def _client(db, router, role: str = "technician") -> TestClient:
    from gdx_dispatch.core.modules import require_module

    app = FastAPI()
    app.include_router(router)
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


def _seed(db, *, tech_active: bool = True, lifecycle: str = "scheduled",
          scheduled_at: datetime | None = None, assigned_to: str = TECH) -> Job:
    db.add(Technician(id=TECH, company_id=TENANT, user_id=USER, active=tech_active,
                      created_at=datetime.now(UTC)))
    c = Customer(id=uuid4(), name="Acme", phone="555-1111", company_id=TENANT)
    db.add(c)
    db.commit()
    j = Job(
        id=uuid4(), company_id=TENANT, customer_id=c.id, title="Spring",
        description="d", scheduled_at=scheduled_at or _noon_today(),
        assigned_to=assigned_to, dispatch_status="assigned",
        lifecycle_stage=lifecycle,
    )
    db.add(j)
    db.commit()
    return j


# ── 1. deactivated tech: refused at the gate, not silently unstamped ──


def test_deactivated_tech_arrival_is_refused_not_silently_unstamped(db):
    job = _seed(db, tech_active=False)
    r = _client(db, mobile_router.router).post(f"/api/mobile/jobs/{job.id.hex}/arrived")
    assert r.status_code == 403, r.text
    assert "deactivated" in r.json()["detail"]
    db.expire_all()
    fresh = db.get(Job, job.id)
    # Nothing moved: no on-site flip without the per-tech stamp beside it.
    assert fresh.arrived_at is None
    assert fresh.dispatch_status == "assigned"
    assert db.query(JobAssignment).count() == 0


def test_deactivated_tech_en_route_and_read_are_refused(db):
    job = _seed(db, tech_active=False)
    client = _client(db, mobile_router.router)
    assert client.post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={}).status_code == 403
    assert client.get(f"/api/mobile/job/{job.id.hex}").status_code == 403


def test_active_tech_arrival_stamps_the_assignment(db):
    """Control: the same request from an active tech succeeds and stamps the
    per-tech row — proves the 403 above is the active flag and nothing else."""
    job = _seed(db, tech_active=True)
    r = _client(db, mobile_router.router).post(f"/api/mobile/jobs/{job.id.hex}/arrived")
    assert r.status_code == 200, r.text
    ja = db.query(JobAssignment).filter(JobAssignment.tech_id == TECH).one()
    assert ja.arrived_at is not None


def test_one_active_row_beside_a_deactivated_one_is_not_refused(db):
    job = _seed(db, tech_active=True)
    db.add(Technician(id="tech-old", company_id=TENANT, user_id=USER, active=False,
                      created_at=datetime.now(UTC) - timedelta(days=30)))
    db.commit()
    r = _client(db, mobile_router.router).get(f"/api/mobile/job/{job.id.hex}")
    assert r.status_code == 200, r.text


def test_manager_with_a_deactivated_tech_row_is_not_refused(db):
    job = _seed(db, tech_active=False)
    r = _client(db, mobile_router.router, role="admin").get(f"/api/mobile/job/{job.id.hex}")
    assert r.status_code == 200, r.text


def test_deactivated_tech_today_says_why_instead_of_an_empty_route(db):
    _seed(db, tech_active=False)
    r = _client(db, mobile_router.router).get("/api/mobile/today?tz=UTC")
    assert r.status_code == 403, r.text
    assert "deactivated" in r.json()["detail"]
    # Office roles share the bottom nav: their Today stays an empty day.
    r = _client(db, mobile_router.router, role="admin").get("/api/mobile/today?tz=UTC")
    assert r.status_code == 200, r.text


def test_deactivated_tech_jobs_tab_says_why_instead_of_an_empty_list(db):
    _seed(db, tech_active=False)
    r = _client(db, mobile_router.router).get("/api/mobile/jobs")
    assert r.status_code == 403, r.text
    assert "deactivated" in r.json()["detail"]


def test_deactivated_tech_cannot_bill_or_quote_from_the_phone(db):
    """The invoicing and quoting routers gate through their own helpers; before
    GDXA-208 a deactivated tech refused on /arrived could still create an
    invoice here (201)."""
    from gdx_dispatch.models.tenant_models import Invoice
    from gdx_dispatch.routers import mobile_invoicing, mobile_quoting

    job = _seed(db, tech_active=False, lifecycle="completed")
    r = _client(db, mobile_invoicing.router).post(f"/api/mobile/jobs/{job.id.hex}/invoice", json={})
    assert r.status_code == 403, r.text
    assert "deactivated" in r.json()["detail"]
    assert db.query(Invoice).count() == 0
    r = _client(db, mobile_quoting.router).post(f"/api/mobile/jobs/{job.id.hex}/quote", json={})
    assert r.status_code == 403, r.text
    assert "deactivated" in r.json()["detail"]


def test_manager_gets_one_answer_on_job_detail_and_billing(db):
    """An admin with an old deactivated tech row is never refused on mobile.py
    (manager early-return), so billing must not refuse them either."""
    from gdx_dispatch.routers import mobile_invoicing, mobile_quoting

    job = _seed(db, tech_active=False, lifecycle="completed")
    assert _client(db, mobile_router.router, role="admin").get(
        f"/api/mobile/job/{job.id.hex}").status_code == 200
    r = _client(db, mobile_invoicing.router, role="admin").post(
        f"/api/mobile/jobs/{job.id.hex}/invoice", json={})
    assert r.status_code == 201, r.text
    r = _client(db, mobile_quoting.router, role="admin").get(f"/api/mobile/jobs/{job.id.hex}/quote")
    # Quoting's own ownership SQL may still say 404 for this seed; what matters
    # here is that the admin is not refused as a deactivated tech.
    assert r.status_code != 403 and "deactivated" not in r.text, r.text


# ── 2. cancelled jobs leave the dated Today route ──────────────────────


def test_cancelled_scheduled_job_is_off_today(db):
    _seed(db, lifecycle="cancelled")
    body = _client(db, mobile_router.router).get("/api/mobile/today?tz=UTC").json()
    assert body["count"] == 0
    assert body["jobs"] == []


def test_cancelled_job_with_an_appointment_is_off_today(db):
    job = _seed(db, lifecycle="cancelled")
    db.add(Appointment(id=uuid4(), company_id=TENANT, job_id=job.id, tech_id=TECH, title="Visit",
                       start_at=_noon_today(), end_at=_noon_today() + timedelta(hours=1)))
    db.commit()
    body = _client(db, mobile_router.router).get("/api/mobile/today?tz=UTC").json()
    assert body["count"] == 0


def test_completed_dated_job_stays_on_today(db):
    """Completed stays on the dated route on purpose (the day's record) —
    and proves the cancelled filter did not empty the query outright."""
    _seed(db, lifecycle="completed")
    body = _client(db, mobile_router.router).get("/api/mobile/today?tz=UTC").json()
    assert body["count"] == 1


def test_visit_cancelled_on_its_own_is_off_today(db):
    """Cancelling one appointment leaves the job live and the row dated; the
    route must still drop it. The job here is dated tomorrow, so the only way
    onto today's route is the cancelled visit."""
    job = _seed(db, scheduled_at=_noon_today() + timedelta(days=1))
    db.add(Appointment(id=uuid4(), company_id=TENANT, job_id=job.id, tech_id=TECH,
                       title="Visit", status="cancelled", start_at=_noon_today(),
                       end_at=_noon_today() + timedelta(hours=1)))
    db.commit()
    client = _client(db, mobile_router.router)
    assert client.get("/api/mobile/today?tz=UTC").json()["count"] == 0
    # Control: the same visit, live, is on the route.
    db.query(Appointment).update({"status": "scheduled"})
    db.commit()
    assert client.get("/api/mobile/today?tz=UTC").json()["count"] == 1


# ── 2b. day summary ─────────────────────────────────────────────────────


def test_cancelled_job_is_not_tomorrows_first_stop(db):
    tomorrow = _noon_today() + timedelta(days=1)
    # day-summary matches jobs.assigned_to against the user id.
    _seed(db, lifecycle="cancelled", scheduled_at=tomorrow, assigned_to=USER)
    client = _client(db, day_summary_router.router)
    assert client.get("/api/mobile/day-summary").json()["next_first_stop"] is None

    live = Job(id=uuid4(), company_id=TENANT, title="Opener", description="d",
               scheduled_at=tomorrow + timedelta(hours=1), assigned_to=USER,
               dispatch_status="assigned", lifecycle_stage="scheduled")
    db.add(live)
    db.commit()
    stop = client.get("/api/mobile/day-summary").json()["next_first_stop"]
    assert stop["id"].replace("-", "") == live.id.hex

