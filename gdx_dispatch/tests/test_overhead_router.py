"""Integration tests for /api/overhead/* (ADR-016)."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.routers import overhead as overhead_router


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

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

    app.include_router(overhead_router.router)
    app.dependency_overrides[overhead_router.get_db] = _override_db
    app.dependency_overrides[overhead_router.get_current_user] = lambda: {
        "sub": "test-user",
        "role": "admin",
        "tenant_id": "tenant-test",
    }
    tc = TestClient(app, raise_server_exceptions=True)
    yield tc
    app.dependency_overrides.clear()
    engine.dispose()


def _payload(**overrides) -> dict:
    body = {
        "label": "Shop rent",
        "category": "rent",
        "amount": "2000.00",
        "cadence": "monthly",
        "start_date": "2025-01-01",
    }
    body.update(overrides)
    return body


def test_create_and_list(client):
    r = client.post("/api/overhead", json=_payload())
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["label"] == "Shop rent"
    assert created["amount"] == "2000.00"
    assert created["source"] == "manual"
    assert created["active"] is True

    r = client.get("/api/overhead")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["obligations"]) == 1
    # one $2000/mo obligation → current monthly total $2000
    assert body["current_monthly_total"] == "2000.00"
    assert "rent" in body["categories"]
    assert "monthly" in body["cadences"]


def test_annual_normalizes_in_summary(client):
    client.post("/api/overhead", json=_payload(
        label="GL insurance", category="insurance", amount="1200.00", cadence="annual"))
    body = client.get("/api/overhead").json()
    # 1200/yr → 100/mo
    assert body["current_monthly_total"] == "100.00"


def test_projection_endpoint_steps_down(client):
    # Rent forever + a loan that ends this year.
    client.post("/api/overhead", json=_payload(
        label="Rent", category="rent", amount="1000.00", cadence="monthly",
        start_date="2020-01-01"))
    # End the loan next month so the step-down lands inside a short horizon.
    today = datetime.now(UTC).date()
    end_y, end_m = today.year, today.month
    client.post("/api/overhead", json=_payload(
        label="Loan", category="loan", amount="500.00", cadence="monthly",
        start_date="2020-01-01", end_date=date(end_y, end_m, 28).isoformat()))

    r = client.get("/api/overhead/projection?horizon_months=3")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["months"]) == 3
    # First month includes the loan, later months don't.
    assert body["months"][0]["total"] == "1500.00"
    assert body["months"][-1]["total"] == "1000.00"
    assert any(sd["ended"] == ["Loan"] for sd in body["step_downs"])
    assert "Outflow only" in body["disclaimer"]


def test_patch_updates_fields(client):
    oid = client.post("/api/overhead", json=_payload()).json()["id"]
    r = client.patch(f"/api/overhead/{oid}", json={"amount": "2500.00", "notes": "raised"})
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == "2500.00"
    assert r.json()["notes"] == "raised"


def test_patch_scheduled_changes_roundtrip(client):
    oid = client.post("/api/overhead", json=_payload()).json()["id"]
    r = client.patch(f"/api/overhead/{oid}", json={
        "scheduled_changes": [{"effective_date": "2027-01-01", "amount": "2100.00"}]
    })
    assert r.status_code == 200, r.text
    sc = r.json()["scheduled_changes"]
    assert sc == [{"effective_date": "2027-01-01", "amount": "2100.00"}]


def test_delete_soft_removes_from_list(client):
    oid = client.post("/api/overhead", json=_payload()).json()["id"]
    r = client.delete(f"/api/overhead/{oid}")
    assert r.status_code == 204, r.text
    body = client.get("/api/overhead").json()
    assert body["obligations"] == []
    assert body["current_monthly_total"] == "0.00"


def test_rejects_bad_cadence(client):
    r = client.post("/api/overhead", json=_payload(cadence="fortnightly"))
    assert r.status_code == 422


def test_rejects_end_before_start(client):
    r = client.post("/api/overhead", json=_payload(
        start_date="2026-01-01", end_date="2025-01-01"))
    assert r.status_code == 400


def test_404_on_missing(client):
    r = client.patch("/api/overhead/00000000-0000-0000-0000-000000000000", json={"amount": "1"})
    assert r.status_code == 404
