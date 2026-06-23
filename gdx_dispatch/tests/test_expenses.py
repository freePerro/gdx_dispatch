from __future__ import annotations

from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Expense, ExpenseLine
from gdx_dispatch.routers import expenses as expenses_router


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # Create module grant tables and seed the required module
    _setup_db = SessionLocal()
    _setup_db.execute(text("""
        CREATE TABLE IF NOT EXISTS tenant_module_grants (
            id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT
        )
    """))
    _setup_db.execute(text("""
        CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT,
            UNIQUE(company_id, module_key)
        )
    """))
    _setup_db.execute(text("""
        INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at)
        VALUES ('g1', 'tenant-test', 'jobs', datetime('now'), datetime('now'))
    """))
    _setup_db.execute(text("""
        INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
        VALUES ('g2', 'tenant-test', 'jobs', datetime('now'), datetime('now'))
    """))
    _setup_db.commit()
    _setup_db.close()

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": "tenant-test"}
        return await call_next(request)

    app.include_router(expenses_router.router)
    app.dependency_overrides[expenses_router.get_db] = _override_db
    app.dependency_overrides[expenses_router.get_current_user] = lambda: {
        "user_id": "test-user",
        "role": "admin",
        "tenant_id": "tenant-test",
    }

    tc = TestClient(app, raise_server_exceptions=True)
    yield tc, SessionLocal
    app.dependency_overrides.clear()
    engine.dispose()


def _create_expense(client: TestClient, **overrides) -> dict:
    payload = {
        "vendor": "Fuel Station",
        "amount": 54.22,
        "date": "2026-03-01",
        "category": "Fuel",
        "description": "Truck fuel",
    }
    payload.update(overrides)
    r = client.post("/api/expenses", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_create_expense(client):
    tc, _ = client
    data = _create_expense(tc)
    assert UUID(data["id"])
    assert data["vendor"] == "Fuel Station"
    assert data["amount"] == 54.22
    assert data["date"] == "2026-03-01"
    assert data["category"] == "Fuel"
    assert data["deleted_at"] is None


def test_create_expense_requires_vendor(client):
    tc, _ = client
    r = tc.post(
        "/api/expenses",
        json={
            "vendor": "",
            "amount": 50,
            "date": "2026-03-01",
            "category": "Fuel",
        },
    )
    assert r.status_code == 422


def test_list_expenses_date_range_filter(client):
    tc, _ = client
    _create_expense(tc, vendor="A", date="2026-03-01", amount=10)
    _create_expense(tc, vendor="B", date="2026-03-15", amount=20)
    _create_expense(tc, vendor="C", date="2026-04-01", amount=30)

    r = tc.get("/api/expenses", params={"start_date": "2026-03-10", "end_date": "2026-03-31"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data) == 1
    assert data[0]["vendor"] == "B"


def test_get_expense_with_lines(client):
    tc, _ = client
    exp = _create_expense(tc, vendor="Parts Co", amount=120.0)

    r1 = tc.post(
        f"/api/expenses/{exp['id']}/lines",
        json={"account": "Parts", "amount": 100.0, "description": "Springs"},
    )
    assert r1.status_code == 201, r1.text
    r2 = tc.post(
        f"/api/expenses/{exp['id']}/lines",
        json={"account": "Tax", "amount": 20.0, "description": "Sales tax"},
    )
    assert r2.status_code == 201, r2.text

    get_r = tc.get(f"/api/expenses/{exp['id']}")
    assert get_r.status_code == 200, get_r.text
    data = get_r.json()
    assert data["id"] == exp["id"]
    assert len(data["lines"]) == 2
    assert data["lines"][0]["account"] in {"Parts", "Tax"}


def test_get_expense_not_found(client):
    tc, _ = client
    r = tc.get(f"/api/expenses/{UUID(int=0)}")
    assert r.status_code == 404


def test_patch_expense(client):
    tc, _ = client
    exp = _create_expense(tc)
    r = tc.patch(
        f"/api/expenses/{exp['id']}",
        json={"vendor": "Updated Vendor", "description": "Updated", "amount": 99.99},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["vendor"] == "Updated Vendor"
    assert data["description"] == "Updated"
    assert data["amount"] == 99.99


def test_patch_expense_not_found(client):
    tc, _ = client
    r = tc.patch(f"/api/expenses/{UUID(int=0)}", json={"vendor": "x"})
    assert r.status_code == 404


def test_delete_expense_soft_delete(client):
    tc, SessionLocal = client
    exp = _create_expense(tc)

    r = tc.delete(f"/api/expenses/{exp['id']}")
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": True}

    list_r = tc.get("/api/expenses")
    assert list_r.status_code == 200
    assert all(item["id"] != exp["id"] for item in list_r.json())

    db = SessionLocal()
    try:
        row = db.execute(select(Expense).where(Expense.id == UUID(exp["id"]))).scalar_one()
        assert row.deleted_at is not None
    finally:
        db.close()


def test_add_expense_line(client):
    tc, SessionLocal = client
    exp = _create_expense(tc)

    r = tc.post(
        f"/api/expenses/{exp['id']}/lines",
        json={"account": "Tools", "amount": 210.45, "description": "New drill"},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert UUID(data["id"])
    assert data["expense_id"] == exp["id"]
    assert data["amount"] == 210.45

    db = SessionLocal()
    try:
        line = db.execute(select(ExpenseLine).where(ExpenseLine.id == UUID(data["id"]))).scalar_one()
        assert line.account == "Tools"
        assert float(line.amount) == 210.45
    finally:
        db.close()


def test_add_expense_line_expense_not_found(client):
    tc, _ = client
    r = tc.post(
        f"/api/expenses/{UUID(int=0)}/lines",
        json={"account": "Tools", "amount": 10.0, "description": "x"},
    )
    assert r.status_code == 404


def test_list_expense_categories(client):
    tc, _ = client
    r = tc.get("/api/expense-categories")
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, list)
    assert "Fuel" in data
    assert "Other" in data


def test_list_expenses_excludes_soft_deleted(client):
    tc, _ = client
    _create_expense(tc, vendor="Keep Me")
    drop = _create_expense(tc, vendor="Drop Me")

    d = tc.delete(f"/api/expenses/{drop['id']}")
    assert d.status_code == 200

    r = tc.get("/api/expenses")
    assert r.status_code == 200
    vendors = [x["vendor"] for x in r.json()]
    assert "Keep Me" in vendors
    assert "Drop Me" not in vendors


def test_expense_routes_registered_in_main_app():
    from gdx_dispatch.app import create_app
    from gdx_dispatch.tests.conftest import app_route_paths

    app = create_app()
    paths = app_route_paths(app)
    assert "/api/expenses" in paths
    assert "/api/expenses/{expense_id}" in paths
    assert "/api/expenses/{expense_id}/lines" in paths
    assert "/api/expense-categories" in paths
