"""Tests for predictive maintenance endpoint."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.equipment_tracking import router as equip_router


@pytest.fixture()
def client() -> TestClient:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    # Module grants table (needed by require_module)
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS tenant_module_grants (
            id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT
        )
    """))
    db.execute(text("""
        INSERT INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at)
        VALUES ('g1', 't1', 'equipment_tracking', :now, :now)
    """), {"now": datetime.now(timezone.utc).isoformat()})

    # Create tables
    db.execute(text("""
        CREATE TABLE equipment_assets (
            id TEXT PRIMARY KEY, tenant_id TEXT, customer_id TEXT,
            equipment_type TEXT, manufacturer TEXT, model TEXT,
            serial_number TEXT, warranty_expires_on TEXT, install_date TEXT, notes TEXT,
            created_at TEXT, updated_at TEXT, deleted_at TEXT
        )
    """))
    db.execute(text("""
        CREATE TABLE equipment_asset_history (
            id TEXT PRIMARY KEY, tenant_id TEXT, equipment_id TEXT,
            service_type TEXT, service_date TEXT, technician_id TEXT, notes TEXT
        )
    """))

    now = datetime.now(timezone.utc)
    # Equipment serviced recently (low risk)
    db.execute(text("""
        INSERT INTO equipment_assets (id, tenant_id, customer_id, equipment_type, manufacturer, model, created_at, updated_at)
        VALUES (:id, :tid, :cid, :et, :mfg, :mdl, :ca, :ua)
    """), {"id": "eq-new", "tid": "t1", "cid": "c1", "et": "opener", "mfg": "LiftMaster", "mdl": "8500", "ca": now.isoformat(), "ua": now.isoformat()})

    # Equipment not serviced in 2 years (high risk for roller)
    old_date = (now - timedelta(days=800)).isoformat()
    db.execute(text("""
        INSERT INTO equipment_assets (id, tenant_id, customer_id, equipment_type, manufacturer, model, created_at, updated_at)
        VALUES (:id, :tid, :cid, :et, :mfg, :mdl, :ca, :ua)
    """), {"id": "eq-old", "tid": "t1", "cid": "c2", "et": "roller", "mfg": "Wayne Dalton", "mdl": "Classic", "ca": old_date, "ua": old_date})

    db.commit()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": "t1"}
        request.state.current_user = {"role": "admin", "user_id": "u1"}
        return await call_next(request)

    app.include_router(equip_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"role": "admin", "user_id": "u1"}

    return TestClient(app)


def test_predictive_maintenance_returns_flagged_equipment(client: TestClient) -> None:
    resp = client.get("/api/equipment/predictive-maintenance?risk_threshold=0.5")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    # The old roller (800 days / 730 interval = ~1.1 risk) should be flagged
    flagged_ids = [item["equipment_id"] for item in data]
    assert "eq-old" in flagged_ids


def test_predictive_maintenance_filters_by_threshold(client: TestClient) -> None:
    # Very high threshold — nothing should be flagged
    resp = client.get("/api/equipment/predictive-maintenance?risk_threshold=2.0")
    assert resp.status_code == 200
    assert len(resp.json()) == 0


def test_predictive_maintenance_has_recommendation(client: TestClient) -> None:
    resp = client.get("/api/equipment/predictive-maintenance?risk_threshold=0.5")
    data = resp.json()
    for item in data:
        assert "recommendation" in item
        assert item["risk_score"] >= 0.5
        assert item["expected_interval_days"] > 0
