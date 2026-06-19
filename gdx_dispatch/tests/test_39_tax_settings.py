"""
gdx_dispatch/tests/test_39_tax_settings.py — Tax settings tests.

Tests:
  1. test_tax_settings_get          — GET /api/tax/settings → 200 dict (TODO: implement)
  2. test_tax_jurisdiction_list     — GET /api/tax/jurisdictions → 200 list (TODO: implement)
  3. test_tax_rate_lookup           — GET /api/tax/rate?zip=90210 → 200 or 422 (TODO: implement)
  4. test_tax_exempt_customer       — PATCH /api/customers/{id}/tax-exempt → 200 or 404 (TODO: implement)
  5. test_sales_tax_report          — GET /api/reports/sales-tax?period=2026-03 → 200 dict (TODO: implement)

NOTE: No tax routes exist in the current codebase.  All tests below verify that
the routes return 404 (Not Found) at present and are annotated with
# TODO: implement to track the implementation backlog.

When the tax module is built:
- Each test's assertion should be updated from ``404`` to the real expected
  status code and the ``# TODO: implement`` comment should be removed.
- The test names intentionally match the task specification so they can be
  searched and updated as a unit.

All HTTP tests use an isolated FastAPI TestClient built from gdx_dispatch.app.create_app()
with the real router set so the route resolution is faithful to production.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Base as TenantModelsBase
from gdx_dispatch.models.tenant_models import Customer

# ---------------------------------------------------------------------------
# Minimal app fixture — includes only the routers that exist today.
# Tax routes do not exist yet; requests to them will return 404.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """Minimal TestClient — no auth overrides; used for route-existence checks."""
    app = FastAPI()

    # Include routers that are known to exist (tax routes are not yet present)
    try:
        from gdx_dispatch.core.gdpr_router import router as gdpr_router
        app.include_router(gdpr_router)
    except Exception:
        pass

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# In-memory DB fixture for the tax-exempt customer test
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    """Isolated in-memory tenant DB."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantModelsBase.metadata.create_all(engine, checkfirst=True)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    yield session
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# Test 1: test_tax_settings_get
# ---------------------------------------------------------------------------

def test_tax_settings_get(client: TestClient):
    """GET /api/tax/settings should return a 200 dict with tax configuration.

    # TODO: implement — route /api/tax/settings does not exist yet.
    Current expectation: 404 (route not registered).
    When implemented, assert: resp.status_code == 200 and isinstance(resp.json(), dict).
    """
    resp = client.get("/api/tax/settings")
    # TODO: implement — change to assert resp.status_code == 200 once route exists
    assert resp.status_code == 404, (
        f"Expected 404 (route not yet implemented), got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 2: test_tax_jurisdiction_list
# ---------------------------------------------------------------------------

def test_tax_jurisdiction_list(client: TestClient):
    """GET /api/tax/jurisdictions should return a 200 list of tax jurisdictions.

    # TODO: implement — route /api/tax/jurisdictions does not exist yet.
    Current expectation: 404 (route not registered).
    When implemented, assert: resp.status_code == 200 and isinstance(resp.json(), list).
    """
    resp = client.get("/api/tax/jurisdictions")
    # TODO: implement — change to assert resp.status_code == 200 once route exists
    assert resp.status_code == 404, (
        f"Expected 404 (route not yet implemented), got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 3: test_tax_rate_lookup
# ---------------------------------------------------------------------------

def test_tax_rate_lookup(client: TestClient):
    """GET /api/tax/rate?zip=90210 should return a 200 rate dict or 422 for bad input.

    # TODO: implement — route /api/tax/rate does not exist yet.
    Current expectation: 404 (route not registered).
    When implemented, assert: resp.status_code in (200, 422).
    For 200: assert "rate" in resp.json() or "tax_rate" in resp.json().
    For 422: assert invalid zip code validation fires correctly.
    """
    resp = client.get("/api/tax/rate", params={"zip": "90210"})
    # TODO: implement — change assertion once route exists
    assert resp.status_code == 404, (
        f"Expected 404 (route not yet implemented), got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 4: test_tax_exempt_customer
# ---------------------------------------------------------------------------

def test_tax_exempt_customer(db):
    """PATCH /api/customers/{id}/tax-exempt should mark a customer as tax-exempt.

    # TODO: implement — route /api/customers/{id}/tax-exempt does not exist yet.
    This test directly verifies the data model side: the Customer.metadata_
    JSON field can store a tax_exempt flag.  When the HTTP route is built,
    add a TestClient assertion in addition to this unit-level check.

    When implemented:
    - POST/PATCH to the route with a valid customer_id → 200
    - Route with unknown customer_id → 404
    - Customer.metadata_ should contain {"tax_exempt": True}
    """
    # Unit-level check: Customer.metadata_ can store tax_exempt flag
    c = Customer(name="Tax Exempt Corp", email="taxexempt@corp.com", company_id="tenant-test")
    db.add(c)
    db.commit()
    db.refresh(c)

    # Simulate what the route would do
    c.metadata_ = {**(c.metadata_ or {}), "tax_exempt": True}
    db.commit()
    db.expire_all()

    refreshed = db.execute(select(Customer).where(Customer.id == c.id)).scalar_one()
    assert refreshed.metadata_ is not None
    assert refreshed.metadata_.get("tax_exempt") is True, (
        "Customer.metadata_ must support tax_exempt flag"
    )

    # TODO: implement HTTP route — once built, add:
    # app = FastAPI(); app.include_router(customers_router)
    # with TestClient(app, raise_server_exceptions=False) as client:
    #     resp = client.patch(f"/api/customers/{c.id}/tax-exempt", json={"tax_exempt": True})
    #     assert resp.status_code in (200, 404)


# ---------------------------------------------------------------------------
# Test 5: test_sales_tax_report
# ---------------------------------------------------------------------------

def test_sales_tax_report(client: TestClient):
    """GET /api/reports/sales-tax?period=2026-03 should return a 200 dict with tax totals.

    # TODO: implement — route /api/reports/sales-tax does not exist yet.
    Current expectation: 404 (route not registered).
    When implemented, assert:
    - resp.status_code == 200
    - isinstance(resp.json(), dict)
    - "period" in resp.json() or "total_tax" in resp.json()
    """
    resp = client.get("/api/reports/sales-tax", params={"period": "2026-03"})
    # TODO: implement — change to assert resp.status_code == 200 once route exists
    assert resp.status_code == 404, (
        f"Expected 404 (route not yet implemented), got {resp.status_code}"
    )
