"""S1-A6 — POST /api/mobile/today/reorder.

Live mode rotates scheduled slots among the calling tech's appointments.
dispatch_approval mode is gated 501 until the pending-changes table
lands. Audit row asserts before/after order on every successful reorder.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Appointment, AppSettings, Customer, Job, Technician
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


def _seed_route(db, n=3):
    """Create N appointments in 1-hour slots starting 9am, all on today."""
    db.add(Technician(id=TECH, company_id=TENANT, user_id=USER, active=True))
    today = _now().replace(hour=9, minute=0, second=0, microsecond=0)
    appts = []
    for i in range(n):
        c = Customer(
            id=uuid4(),
            name=f"Customer {i}",
            phone="555-0000",
            email=f"c{i}@x.com",
            address=f"{i} Main",
            company_id=TENANT,
        )
        db.add(c)
        j = Job(
            id=uuid4(),
            company_id=TENANT,
            customer_id=c.id,
            title=f"Job {i}",
            description="",
            scheduled_at=today + timedelta(hours=i),
            assigned_to=TECH,
            dispatch_status="assigned",
        )
        db.add(j)
        a = Appointment(
            id=uuid4(),
            company_id=TENANT,
            job_id=j.id,
            customer_id=c.id,
            tech_id=TECH,
            title=f"Job {i}",
            start_at=today + timedelta(hours=i),
            end_at=today + timedelta(hours=i, minutes=45),
        )
        db.add(a)
        appts.append((a, j))
    db.commit()
    return appts


def _set_authority(db, value: str) -> None:
    row = db.query(AppSettings).first()
    if row is None:
        row = AppSettings(tenant_mobile_settings={"tech_mobile.drag_reorder_authority": value})
        db.add(row)
    else:
        overrides = dict(row.tenant_mobile_settings or {})
        overrides["tech_mobile.drag_reorder_authority"] = value
        row.tenant_mobile_settings = overrides
    db.commit()


# ── Live mode ─────────────────────────────────────────────────────────


class TestLive:
    def test_rotates_slots_among_appointments(self, app_and_db):
        client, db = app_and_db
        appts = _seed_route(db, n=3)
        # Slots before reorder: A=9am, B=10am, C=11am.
        # Reorder to [C, A, B] → C@9am, A@10am, B@11am.
        new_order = [str(appts[2][0].id), str(appts[0][0].id), str(appts[1][0].id)]
        r = client.post(
            "/api/mobile/today/reorder", json={"appointment_ids": new_order}
        )
        assert r.status_code == 200, r.text
        assert r.json()["changed"] is True

        db.expire_all()
        # The appointment that was C (originally 11am) now occupies 9am.
        c_appt = db.query(Appointment).filter(Appointment.id == appts[2][0].id).one()
        assert c_appt.start_at.hour == 9
        # The Job linked to C also has its scheduled_at updated.
        c_job = db.query(Job).filter(Job.id == appts[2][1].id).one()
        assert c_job.scheduled_at.hour == 9

    def test_no_op_reorder_does_not_audit(self, app_and_db):
        client, db = app_and_db
        appts = _seed_route(db, n=3)
        same_order = [str(appts[0][0].id), str(appts[1][0].id), str(appts[2][0].id)]
        r = client.post(
            "/api/mobile/today/reorder", json={"appointment_ids": same_order}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["changed"] is False
        assert (
            db.query(AuditLog)
            .filter(AuditLog.action == "mobile_today_reordered")
            .count()
            == 0
        )

    def test_audit_records_before_after(self, app_and_db):
        client, db = app_and_db
        appts = _seed_route(db, n=3)
        new_order = [str(appts[1][0].id), str(appts[0][0].id), str(appts[2][0].id)]
        client.post("/api/mobile/today/reorder", json={"appointment_ids": new_order})
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.action == "mobile_today_reordered")
            .all()
        )
        assert len(rows) == 1
        details = rows[0].details
        assert details["after"] == new_order
        assert details["authority"] == "live"
        assert len(details["before"]) == 3


# ── Validation ────────────────────────────────────────────────────────


class TestValidation:
    def test_missing_appointment_400(self, app_and_db):
        client, db = app_and_db
        appts = _seed_route(db, n=3)
        # Drop one — server must refuse partial reorderings.
        partial = [str(appts[0][0].id), str(appts[1][0].id)]
        r = client.post(
            "/api/mobile/today/reorder", json={"appointment_ids": partial}
        )
        assert r.status_code == 400
        assert "every today-appointment" in r.json()["detail"]

    def test_duplicate_id_400(self, app_and_db):
        client, db = app_and_db
        appts = _seed_route(db, n=3)
        bad = [str(appts[0][0].id), str(appts[0][0].id), str(appts[2][0].id)]
        r = client.post(
            "/api/mobile/today/reorder", json={"appointment_ids": bad}
        )
        assert r.status_code == 400

    def test_unknown_appointment_400(self, app_and_db):
        client, db = app_and_db
        appts = _seed_route(db, n=3)
        bad = [str(appts[0][0].id), str(uuid4()), str(appts[2][0].id)]
        r = client.post(
            "/api/mobile/today/reorder", json={"appointment_ids": bad}
        )
        assert r.status_code == 400

    def test_no_route_no_appointments(self, app_and_db):
        client, db = app_and_db
        db.add(Technician(id=TECH, company_id=TENANT, user_id=USER, active=True))
        db.commit()
        r = client.post("/api/mobile/today/reorder", json={"appointment_ids": []})
        assert r.status_code == 200
        assert r.json()["changed"] is False


# ── Authority gate ────────────────────────────────────────────────────


class TestAuthority:
    def test_dispatch_approval_returns_501(self, app_and_db):
        client, db = app_and_db
        appts = _seed_route(db, n=2)
        _set_authority(db, "dispatch_approval")
        r = client.post(
            "/api/mobile/today/reorder",
            json={
                "appointment_ids": [
                    str(appts[1][0].id),
                    str(appts[0][0].id),
                ]
            },
        )
        assert r.status_code == 501
        assert "not yet implemented" in r.json()["detail"]

    def test_unknown_authority_400(self, app_and_db):
        client, db = app_and_db
        appts = _seed_route(db, n=2)
        # Force an invalid authority directly on the column (bypassing
        # the validator the admin PUT enforces). Helper must reject.
        row = db.query(AppSettings).first() or AppSettings(tenant_mobile_settings={})
        row.tenant_mobile_settings = {"tech_mobile.drag_reorder_authority": "bogus"}
        if row not in db:
            db.add(row)
        db.commit()
        r = client.post(
            "/api/mobile/today/reorder",
            json={
                "appointment_ids": [
                    str(appts[1][0].id),
                    str(appts[0][0].id),
                ]
            },
        )
        assert r.status_code == 400
