"""Labor-matrix lines must arrive at the profit panel with cost_snapshot set.

Doug 2026-05-05. EST-000026 surfaced that addFromLabor pushed lines with
cost_snapshot=null, which the profit panel filter dropped — so labor revenue
+ labor cost both vanished from the blended margin.

Fix: when an estimate line has labor_price_item_id and no explicit cost,
backend stamps cost_snapshot from PricingSettings.loaded_labor_cost_per_hour
× estimated_man_hours. Even rate=0 writes cost=0 (not null) so the panel
always includes the line.

These tests gate the route behavior, not just the helper, per S105 feedback
(require_role test signature lesson — pair every gate with an HTTP-level test).
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.labor_pricing import LaborPriceItem
from gdx_dispatch.models.pricing_engine import PricingSettings
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.estimates import router


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    _setup = Session()
    _setup.execute(text("""
        CREATE TABLE IF NOT EXISTS tenant_module_grants (
            id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT
        )
    """))
    _setup.execute(text("""
        CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT,
            UNIQUE(company_id, module_key)
        )
    """))
    _setup.execute(text("""
        INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at)
        VALUES ('g1', 'tenant-test', 'estimates', datetime('now'), datetime('now'))
    """))
    _setup.execute(text("""
        INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
        VALUES ('g2', 'tenant-test', 'estimates', datetime('now'), datetime('now'))
    """))
    _setup.commit()
    _setup.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": "tenant-test"}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1",
        "role": "admin",
        "tenant_id": "tenant-test",
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._sessionmaker = Session
    yield tc
    app.dependency_overrides.clear()
    engine.dispose()


def _seed_settings(client: TestClient, *, rate: Decimal) -> None:
    db = client._sessionmaker()
    try:
        db.add(PricingSettings(
            id=uuid4(),
            volume_discount_enabled=False,
            loaded_labor_cost_per_hour=rate,
        ))
        db.commit()
    finally:
        db.close()


def _seed_customer(client: TestClient) -> str:
    db = client._sessionmaker()
    try:
        c = Customer(name="Test Cust", email="x@y.z", company_id="tenant-test")
        db.add(c); db.commit(); db.refresh(c)
        return str(c.id)
    finally:
        db.close()


def _seed_labor_row(client: TestClient) -> str:
    db = client._sessionmaker()
    try:
        row = LaborPriceItem(
            id=uuid4(),
            description="12x12 Install",
            service_type="install",
            width_ft=12, height_ft=12,
            flat_price=Decimal("700.00"),
            assumed_man_hours=Decimal("7.00"),
        )
        db.add(row); db.commit()
        return str(row.id)
    finally:
        db.close()


def _post_estimate_with_labor_line(client: TestClient, *, labor_id: str, hours: float, sell: float) -> dict:
    customer_id = _seed_customer(client)
    payload = {
        "customer_id": customer_id,
        "label": "Labor cost test",
        "line_items": [
            {
                "description": "Install 12x12",
                "category": "Labor",
                "quantity": 12,
                "unit_price": sell,
                "labor_price_item_id": labor_id,
                "estimated_man_hours": hours,
            },
        ],
    }
    r = client.post("/api/estimates", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_labor_line_gets_cost_snapshot_from_tenant_rate(client: TestClient):
    """Cost = hours × rate × quantity. Sell = flat_price × quantity. Doug
    2026-05-07 — qty multiplier added in S6 because pre-fix scheduler and
    variance both undercounted multi-unit installs (a 4-door job costed
    like a 1-door job). Both cost and sell are now qty-aware end-to-end."""
    _seed_settings(client, rate=Decimal("35.00"))
    labor_id = _seed_labor_row(client)
    # Fixture posts qty=12 — assertions reflect qty-aware math.
    est = _post_estimate_with_labor_line(client, labor_id=labor_id, hours=7.0, sell=700.0)

    line = est["lines"][0]
    # cost = 7h × $35 × 12 = $2,940. sell = $700 × 12 = $8,400.
    assert line["cost_snapshot"] == pytest.approx(2940.0)
    # margin = (8400 - 2940) / 8400 = 0.65
    assert line["margin_pct_snapshot"] == pytest.approx(0.65)
    assert line["pricing_source"] == "labor_matrix"
    assert line["labor_price_item_id"] == labor_id
    # estimated_man_hours stored per-row from matrix (qty multiplies at
    # read-time downstream — appointments, variance).
    assert line["estimated_man_hours"] == pytest.approx(7.0)
    # Sell now matrix-authoritative: client sent 700 but flat_price wins.
    assert line["unit_price"] == pytest.approx(700.0)
    assert line["line_total"] == pytest.approx(8400.0)


def test_labor_line_zero_rate_writes_zero_not_null(client: TestClient):
    """Guards the silent-drop regression. Cost=0 when rate unset, NOT null."""
    _seed_settings(client, rate=Decimal("0"))
    labor_id = _seed_labor_row(client)
    est = _post_estimate_with_labor_line(client, labor_id=labor_id, hours=7.0, sell=700.0)

    line = est["lines"][0]
    assert line["cost_snapshot"] is not None, "labor line must not have null cost — that was the bug"
    assert line["cost_snapshot"] == pytest.approx(0.0)
    assert line["margin_pct_snapshot"] == pytest.approx(1.0)    # 100% margin when cost=0
    assert line["pricing_source"] == "labor_matrix"


def test_labor_line_appears_in_profit_panel_filter(client: TestClient):
    """HTTP round-trip: line must satisfy `cost_snapshot != null && margin_pct_snapshot != null`
    so EstimateProfitPanel.vue:120 includes it."""
    _seed_settings(client, rate=Decimal("35.00"))
    labor_id = _seed_labor_row(client)
    est = _post_estimate_with_labor_line(client, labor_id=labor_id, hours=7.0, sell=700.0)

    # Re-fetch through the GET endpoint (the path the frontend actually hits).
    r = client.get(f"/api/estimates/{est['id']}")
    assert r.status_code == 200
    fetched = r.json()
    labor_lines = [ln for ln in fetched["lines"] if ln["labor_price_item_id"]]
    assert len(labor_lines) == 1
    ln = labor_lines[0]
    # The profit panel filter — both must be non-null.
    assert ln["cost_snapshot"] is not None
    assert ln["margin_pct_snapshot"] is not None


def test_matrix_row_overrides_client_supplied_cost(client: TestClient):
    """Doug 2026-05-07 / EST-000030 retro: matrix flat_price wins over any
    client-supplied cost or unit_price. Pre-fix behavior allowed an
    operator-typed cost to set cost_snapshot directly; that path is what
    let the $91k cascade through (synthetic cost from the bad assumed_man_hours
    field flowed into the engine and overwrote sell). Now: matrix is truth."""
    _seed_settings(client, rate=Decimal("35.00"))
    labor_id = _seed_labor_row(client)
    customer_id = _seed_customer(client)
    payload = {
        "customer_id": customer_id,
        "label": "Explicit cost ignored",
        "line_items": [
            {
                "description": "Install 12x12 (custom cost ignored)",
                "category": "Labor",
                "quantity": 1,
                "unit_price": 9999.99,  # malicious / typo'd — must not win
                "cost": 500.0,            # ignored — matrix derives cost
                "labor_price_item_id": labor_id,
                "estimated_man_hours": 99.0,  # ignored — matrix hours win
            },
        ],
    }
    r = client.post("/api/estimates", json=payload)
    assert r.status_code == 201
    line = r.json()["lines"][0]
    # Matrix row from _seed_labor_row: flat_price=$700, hours=7. Rate=$35.
    assert line["unit_price"] == pytest.approx(700.0)
    assert line["cost_snapshot"] == pytest.approx(245.0)  # 7 × 35 × 1
    assert line["estimated_man_hours"] == pytest.approx(7.0)
    assert line["pricing_source"] == "labor_matrix"
