"""Tests for the job costing router (markup rules, price calc, cost breakdown)."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.job_costing import MarkupRule, router


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
            "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
            "VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))"
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
# Markup rule CRUD
# ---------------------------------------------------------------------------


def test_create_markup_rule(client: TestClient):
    r = client.post(
        "/api/costing/markup-rules",
        json={"category": "parts", "markup_percent": 40, "minimum_margin_percent": 0},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"]
    assert data["category"] == "parts"
    assert data["markup_percent"] == 40.0
    assert data["active"] is True
    assert data["company_id"] == "tenant-test"


def test_unique_category_per_tenant(client: TestClient):
    r1 = client.post(
        "/api/costing/markup-rules",
        json={"category": "labor", "markup_percent": 25},
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/api/costing/markup-rules",
        json={"category": "labor", "markup_percent": 30},
    )
    assert r2.status_code == 409
    assert "labor" in r2.json()["detail"].lower()


def test_list_rules_tenant_scoped():
    a = _make_client(tenant_id="tenant-a")
    b = _make_client(tenant_id="tenant-b")
    try:
        a.post("/api/costing/markup-rules", json={"category": "parts", "markup_percent": 40})
        b.post("/api/costing/markup-rules", json={"category": "parts", "markup_percent": 55})

        list_a = a.get("/api/costing/markup-rules").json()
        list_b = b.get("/api/costing/markup-rules").json()
        assert len(list_a) == 1 and list_a[0]["markup_percent"] == 40.0
        assert len(list_b) == 1 and list_b[0]["markup_percent"] == 55.0
        assert list_a[0]["company_id"] == "tenant-a"
        assert list_b[0]["company_id"] == "tenant-b"
    finally:
        a.app.dependency_overrides.clear()
        b.app.dependency_overrides.clear()
        a._engine.dispose()  # type: ignore[attr-defined]
        b._engine.dispose()  # type: ignore[attr-defined]


def test_soft_delete_rule(client: TestClient):
    created = client.post(
        "/api/costing/markup-rules",
        json={"category": "equipment", "markup_percent": 20},
    ).json()
    r = client.delete(f"/api/costing/markup-rules/{created['id']}")
    assert r.status_code == 204

    listed = client.get("/api/costing/markup-rules").json()
    assert all(rule["id"] != created["id"] for rule in listed)

    dep = client.app.dependency_overrides[get_db]
    db = next(dep())
    try:
        row = db.execute(
            select(MarkupRule).where(MarkupRule.id == UUID(created["id"]))
        ).scalar_one()
        assert row.deleted_at is not None
        assert row.active is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Price calculator
# ---------------------------------------------------------------------------


def test_calculate_price_uses_rule(client: TestClient):
    client.post(
        "/api/costing/markup-rules",
        json={"category": "parts", "markup_percent": 40, "minimum_margin_percent": 0},
    )
    r = client.post(
        "/api/costing/calculate-price",
        json={"category": "parts", "cost": 100},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["markup_percent"] == 40.0
    assert data["suggested_price"] == 140.0
    assert data["rule_id"] is not None


def test_calculate_price_default_when_no_rule(client: TestClient):
    r = client.post(
        "/api/costing/calculate-price",
        json={"category": "unknown_category", "cost": 100},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    # Default 35% markup => 135
    assert data["markup_percent"] == 35.0
    assert data["suggested_price"] == 135.0
    assert data["rule_id"] is None


def test_minimum_margin_floor(client: TestClient):
    # 20% markup is too low given 50% min margin; floor raises price to hit 50% margin.
    # cost=100, 50% min margin => price = 100 / (1 - 0.5) = 200
    client.post(
        "/api/costing/markup-rules",
        json={
            "category": "premium",
            "markup_percent": 20,
            "minimum_margin_percent": 50,
        },
    )
    r = client.post(
        "/api/costing/calculate-price",
        json={"category": "premium", "cost": 100},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["suggested_price"] == 200.0
    assert data["minimum_margin_percent"] == 50.0


# ---------------------------------------------------------------------------
# Cost breakdown for missing job
# ---------------------------------------------------------------------------


def test_get_costing_for_missing_job(client: TestClient):
    r = client.get(f"/api/costing/jobs/{uuid4()}")
    # Should return zeroed structure, NOT 500
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["labor"]["total"] == 0.0
    assert data["parts"]["total"] == 0.0
    assert data["total_cost"] == 0.0
    assert data["invoiced_amount"] == 0.0
    assert data["profit"] == 0.0
    assert data["margin_percent"] == 0.0


# ---------------------------------------------------------------------------
# Patch / update
# ---------------------------------------------------------------------------


def test_patch_updates_markup_rule(client: TestClient):
    created = client.post(
        "/api/costing/markup-rules",
        json={"category": "parts", "markup_percent": 40},
    ).json()
    r = client.patch(
        f"/api/costing/markup-rules/{created['id']}",
        json={"markup_percent": 55, "minimum_margin_percent": 10},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["markup_percent"] == 55.0
    assert data["minimum_margin_percent"] == 10.0


def test_catalog_pricing_endpoint(client: TestClient):
    client.post(
        "/api/costing/markup-rules",
        json={"category": "parts", "markup_percent": 40},
    )
    client.post(
        "/api/costing/markup-rules",
        json={"category": "labor", "markup_percent": 25},
    )
    r = client.get("/api/costing/catalog-pricing")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    cats = {row["category"] for row in data}
    assert cats == {"parts", "labor"}
