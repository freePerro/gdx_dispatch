"""Tests for the payroll router's commission rate routes.

The summary, per-tech and export routes and their tests were deleted in
GDXA-1560.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.payroll import router


def _make_client(tenant_id: str = "tenant-test") -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS company_module_grants (
                id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
                granted_at TEXT, created_at TEXT, expires_at TEXT,
                UNIQUE(company_id, module_key)
            )
            """
        )
    )
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'timeclock', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g2-{tenant_id}", "tid": tenant_id},
    )
    setup.commit()
    setup.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": tenant_id}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1",
        "sub": "user-1",
        "role": "admin",
        "tenant_id": tenant_id,
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    return tc


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Commission rate CRUD
# ---------------------------------------------------------------------------
def test_create_commission_rate(client: TestClient):
    r = client.post(
        "/api/payroll/commission-rates",
        json={"tech_id": "tech-1", "rate_type": "percent", "rate_value": 12.5},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["tech_id"] == "tech-1"
    assert data["rate_type"] == "percent"
    assert data["rate_value"] == 12.5
    assert data["active"] is True
    assert data["effective_until"] is None
    assert data["company_id"] == "tenant-test"


def test_new_rate_expires_prior(client: TestClient):
    a = client.post(
        "/api/payroll/commission-rates",
        json={"tech_id": "tech-2", "rate_type": "percent", "rate_value": 10},
    ).json()
    b = client.post(
        "/api/payroll/commission-rates",
        json={"tech_id": "tech-2", "rate_type": "percent", "rate_value": 15},
    ).json()
    assert a["id"] != b["id"]

    # Fetch all rates for tech-2 including inactive ones
    all_rates = client.get(
        "/api/payroll/commission-rates",
        params={"tech_id": "tech-2", "active_only": "false"},
    ).json()
    by_id = {r["id"]: r for r in all_rates}
    assert by_id[a["id"]]["effective_until"] is not None
    assert by_id[a["id"]]["active"] is False
    assert by_id[b["id"]]["effective_until"] is None
    assert by_id[b["id"]]["active"] is True


def test_list_active_rates(client: TestClient):
    client.post(
        "/api/payroll/commission-rates",
        json={"tech_id": "tech-3", "rate_type": "flat", "rate_value": 50},
    )
    client.post(
        "/api/payroll/commission-rates",
        json={"tech_id": "tech-4", "rate_type": "hourly", "rate_value": 5},
    )
    active = client.get("/api/payroll/commission-rates").json()
    assert len(active) == 2
    tech_ids = {r["tech_id"] for r in active}
    assert tech_ids == {"tech-3", "tech-4"}


def test_rate_type_validation(client: TestClient):
    r = client.post(
        "/api/payroll/commission-rates",
        json={"tech_id": "tech-x", "rate_type": "bogus", "rate_value": 10},
    )
    assert r.status_code == 422


def test_rate_value_bounds_negative(client: TestClient):
    r = client.post(
        "/api/payroll/commission-rates",
        json={"tech_id": "tech-x", "rate_type": "percent", "rate_value": -1},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------
def test_tenant_scope():
    c1 = _make_client(tenant_id="tenant-a")
    c2 = _make_client(tenant_id="tenant-b")
    try:
        r1 = c1.post(
            "/api/payroll/commission-rates",
            json={"tech_id": "techA", "rate_type": "percent", "rate_value": 10},
        )
        assert r1.status_code == 201
        r2 = c2.post(
            "/api/payroll/commission-rates",
            json={"tech_id": "techB", "rate_type": "percent", "rate_value": 20},
        )
        assert r2.status_code == 201

        list_a = c1.get("/api/payroll/commission-rates").json()
        list_b = c2.get("/api/payroll/commission-rates").json()
        assert len(list_a) == 1
        assert len(list_b) == 1
        assert list_a[0]["tech_id"] == "techA"
        assert list_b[0]["tech_id"] == "techB"
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]
