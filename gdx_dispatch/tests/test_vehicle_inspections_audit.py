"""Vehicle inspection create and delete write an audit row (GDXA-1591).

``create_inspection`` and ``delete_inspection`` committed with no audit call,
so who logged or removed an inspection could not be reconstructed
(invariant #1). The audit row is flushed before ``db.commit()``, so it shares
the mutation's transaction.

Through the real router; each assertion reads ``audit_logs`` back.
"""
from __future__ import annotations

from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import VehicleInspection
from gdx_dispatch.routers import vehicle_inspections as inspections_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"
USER = "tech-1"


@pytest.fixture
def client_and_db(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    app = FastAPI()
    app.include_router(inspections_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": USER, "tenant_id": TENANT, "role": "technician",
    }

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "test"}
        request.state.user = {"user_id": USER, "tenant_id": TENANT}
        return await call_next(request)

    yield TestClient(app), db
    db.close()
    engine.dispose()


def _audits(db, action: str) -> list[AuditLog]:
    db.expire_all()
    return list(db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all())


def _create(client) -> str:
    r = client.post("/api/vehicle-inspections", json={
        "vehicle_id": "van-7", "vehicle_label": "Van 7",
        "inspection_type": "pre_trip", "odometer": 48210,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_create_writes_audit_row(client_and_db):
    client, db = client_and_db
    insp_id = _create(client)

    (row,) = _audits(db, "create_vehicle_inspection")
    assert row.entity_type == "vehicle_inspection"
    assert row.entity_id == insp_id
    assert row.user_id == USER
    assert row.tenant_id == TENANT
    assert row.details == {
        "inspection_type": "pre_trip", "vehicle_id": "van-7", "odometer": 48210,
    }


def test_delete_writes_audit_row(client_and_db):
    client, db = client_and_db
    insp_id = _create(client)

    r = client.delete(f"/api/vehicle-inspections/{insp_id}")
    assert r.status_code == 200, r.text

    (row,) = _audits(db, "delete_vehicle_inspection")
    assert row.entity_type == "vehicle_inspection"
    assert row.entity_id == insp_id
    assert row.user_id == USER
    assert row.tenant_id == TENANT
    assert row.details == {"id": insp_id, "vehicle_id": "van-7"}
    assert db.get(VehicleInspection, UUID(insp_id)).deleted_at is not None


def test_delete_of_missing_inspection_writes_no_audit_row(client_and_db):
    client, db = client_and_db
    r = client.delete("/api/vehicle-inspections/00000000-0000-0000-0000-000000000001")
    assert r.status_code == 404
    assert _audits(db, "delete_vehicle_inspection") == []


def test_failed_audit_takes_the_create_down_with_it(client_and_db, monkeypatch):
    """The audit row rides the same transaction: no trail, no inspection."""
    client, db = client_and_db

    def _boom(*_a, **_k):
        raise SQLAlchemyError("audit insert refused")

    monkeypatch.setattr(inspections_router, "log_audit_event_sync", _boom)
    r = client.post("/api/vehicle-inspections", json={"inspection_type": "pre_trip"})
    assert r.status_code == 500
    db.expire_all()
    assert db.execute(select(VehicleInspection)).scalars().all() == []
