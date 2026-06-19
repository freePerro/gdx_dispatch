"""Sprint tech_mobile S1-B1 + S1-B2 — arrival geo + state-machine guard.

Two surfaces:
- ``_validate_forward_transition`` enforces forward-only dispatch_status
  changes through the mobile state-advance endpoints. Idempotent re-tap
  is allowed (current == target); backward transitions raise 400.
- ``mobile_job_arrived`` accepts an optional ArrivedBody with lat/lng/
  accuracy. Coordinates land in the audit details; Job.arrived_at and
  Appointment.arrived_at are stamped on first arrival.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Appointment, Customer, Job, Technician
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db


TENANT = "tenant-a"
USER = "user-1"
TECH = "tech-1"


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def app_and_db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SessionLocal()

    from gdx_dispatch.core.modules import require_module

    app = FastAPI()
    app.include_router(mobile_router.router)
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": USER,
        "tenant_id": TENANT,
        "role": "technician",
    }
    app.dependency_overrides[require_module("mobile")] = lambda: True

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "test"}
        request.state.tenant_id = TENANT
        request.state.user = {"user_id": USER, "tenant_id": TENANT}
        return await call_next(request)

    client = TestClient(app)
    yield client, s
    s.close()
    engine.dispose()


def _seed_job(db, *, dispatch_status="assigned") -> tuple[Job, Appointment | None]:
    db.add(Technician(id=TECH, company_id=TENANT, user_id=USER, active=True))
    cust = Customer(
        id=uuid4(),
        name="Acme",
        phone="555",
        email="a@x.com",
        address="11 Main",
        company_id=TENANT,
    )
    db.add(cust)
    job = Job(
        id=uuid4(),
        company_id=TENANT,
        customer_id=cust.id,
        title="Fix",
        description="",
        scheduled_at=_now(),
        assigned_to=TECH,
        dispatch_status=dispatch_status,
    )
    db.add(job)
    appt = Appointment(
        id=uuid4(),
        company_id=TENANT,
        job_id=job.id,
        customer_id=cust.id,
        tech_id=TECH,
        title="Today",
        start_at=_now(),
        end_at=_now() + timedelta(hours=1),
    )
    db.add(appt)
    db.commit()
    return job, appt


# ── Pure helper ───────────────────────────────────────────────────────


class TestForwardOnlyHelper:
    def test_forward_advance_passes(self):
        mobile_router._validate_forward_transition("assigned", "en_route")
        mobile_router._validate_forward_transition("en_route", "on_site")
        mobile_router._validate_forward_transition("on_site", "done")

    def test_idempotent_re_tap_passes(self):
        # Tech double-taps "On my way" — must not 400.
        mobile_router._validate_forward_transition("en_route", "en_route")

    def test_backward_transition_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            mobile_router._validate_forward_transition("on_site", "en_route")
        assert exc.value.status_code == 400
        with pytest.raises(HTTPException):
            mobile_router._validate_forward_transition("done", "on_site")

    def test_unknown_target_400(self):
        with pytest.raises(HTTPException) as exc:
            mobile_router._validate_forward_transition("assigned", "yolo")
        assert exc.value.status_code == 400

    def test_unknown_current_allows_any_forward(self):
        # Historical row with mid-flight value — must not block forward
        # progress through the state machine.
        mobile_router._validate_forward_transition("legacy_value", "en_route")


# ── Arrival geo ───────────────────────────────────────────────────────


class TestArrivalGeo:
    def test_arrival_without_body_succeeds(self, app_and_db):
        client, db = app_and_db
        job, _ = _seed_job(db, dispatch_status="en_route")
        r = client.post(f"/api/mobile/jobs/{job.id.hex}/arrived")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dispatch_status"] == "on_site"

    def test_arrival_records_geo_in_audit(self, app_and_db):
        client, db = app_and_db
        job, _ = _seed_job(db, dispatch_status="en_route")
        r = client.post(
            f"/api/mobile/jobs/{job.id.hex}/arrived",
            json={"lat": 46.8738, "lng": -96.7678, "accuracy": 12.5},
        )
        assert r.status_code == 200
        rows = (
            db.query(AuditLog)
            .filter(
                AuditLog.entity_id == job.id.hex,
                AuditLog.action.like("%arrived%"),
            )
            .all()
        )
        # The legacy _audit_state_change writes the lat/lng/accuracy into
        # the details JSON; pick whichever audit row carries them.
        match = [r for r in rows if (r.details or {}).get("lat") is not None]
        assert match, f"expected an arrived audit row with lat; got {[r.details for r in rows]}"
        details = match[0].details
        assert details["lat"] == 46.8738
        assert details["lng"] == -96.7678
        assert details["accuracy"] == 12.5

    def test_arrival_stamps_job_and_appointment_arrived_at(self, app_and_db):
        client, db = app_and_db
        job, appt = _seed_job(db, dispatch_status="en_route")
        client.post(f"/api/mobile/jobs/{job.id.hex}/arrived")
        db.expire_all()
        job_row = db.query(Job).filter(Job.id == job.id).one()
        appt_row = db.query(Appointment).filter(Appointment.id == appt.id).one()
        assert job_row.arrived_at is not None
        assert appt_row.arrived_at is not None

    def test_re_tap_does_not_overwrite_arrived_at(self, app_and_db):
        client, db = app_and_db
        job, _ = _seed_job(db, dispatch_status="en_route")
        client.post(f"/api/mobile/jobs/{job.id.hex}/arrived")
        db.expire_all()
        first = db.query(Job).filter(Job.id == job.id).one().arrived_at
        # Second tap a moment later — Job.arrived_at must NOT advance.
        client.post(f"/api/mobile/jobs/{job.id.hex}/arrived")
        db.expire_all()
        second = db.query(Job).filter(Job.id == job.id).one().arrived_at
        assert first == second


# ── State machine end-to-end ──────────────────────────────────────────


class TestStateMachineEndToEnd:
    def test_assigned_to_en_route_then_arrived_then_done(self, app_and_db):
        client, db = app_and_db
        job, _ = _seed_job(db, dispatch_status="assigned")
        # Forward-only path that a tech walks every job.
        r1 = client.post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={"eta_minutes": 20})
        assert r1.status_code == 200
        r2 = client.post(f"/api/mobile/jobs/{job.id.hex}/arrived")
        assert r2.status_code == 200
        sig = "data:image/png;base64,c2lnbmF0dXJl"  # base64('signature')
        r3 = client.post(
            f"/api/mobile/jobs/{job.id.hex}/complete",
            json={"signature_data": sig, "signed_by": "Cust"},
        )
        assert r3.status_code == 200, r3.text
        assert r3.json()["dispatch_status"] == "done"
        db.expire_all()
        assert db.query(Job).filter(Job.id == job.id).one().dispatch_status == "done"

    def test_en_route_after_done_400s(self, app_and_db):
        client, db = app_and_db
        job, _ = _seed_job(db, dispatch_status="done")
        r = client.post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={"eta_minutes": 5})
        assert r.status_code == 400
        assert "backward" in r.json()["detail"]

    def test_arrived_after_done_400s(self, app_and_db):
        client, db = app_and_db
        job, _ = _seed_job(db, dispatch_status="done")
        r = client.post(f"/api/mobile/jobs/{job.id.hex}/arrived")
        assert r.status_code == 400

    def test_idempotent_en_route_tap(self, app_and_db):
        # A tech double-tapping "On my way" must succeed both times.
        client, db = app_and_db
        job, _ = _seed_job(db, dispatch_status="en_route")
        r1 = client.post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={})
        r2 = client.post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={})
        assert r1.status_code == 200
        assert r2.status_code == 200
