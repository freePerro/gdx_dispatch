"""Tests for pricing feature endpoints (vendor lists, bundles, customer rates, etc.)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.pricing import router as pricing_router


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()

    # Pricing router gates on require_module("estimates") in production.
    # These unit tests don't set up the tenant_module_grants table, so we
    # stub the tenant context via middleware and swallow the router's
    # module-check dependency via an override. We access the dependency
    # from the router's own declared list instead of calling require_module
    # again (which would produce a distinct function identity).
    @app.middleware("http")
    async def _test_context(request: Request, call_next):
        request.state.tenant = {
            "id": "tenant-test",
            "slug": "tenant-test",
            "subscription_status": "active",
        }
        request.state.user = {"id": "test", "role": "admin"}
        return await call_next(request)

    app.include_router(pricing_router)
    app.dependency_overrides[get_current_user] = lambda: {"role": "admin", "user_id": "test"}
    # Disable the router-level module gate — the pricing endpoints
    # themselves don't touch the DB so we can bypass it for unit tests.
    for dep in pricing_router.dependencies:
        app.dependency_overrides[dep.dependency] = lambda: None
    return TestClient(app)


class TestVendorPriceLists:
    def test_import_and_list(self, client: TestClient) -> None:
        resp = client.post("/api/pricing/vendor-lists", json={
            "items": [
                {"vendor_name": "CHI", "sku": "CHI-2283", "description": "16x7 Raised Panel", "cost": 450.00},
                {"vendor_name": "CHI", "sku": "CHI-4251", "description": "8x7 Flush", "cost": 320.00},
            ]
        })
        assert resp.status_code == 200
        assert resp.json()["imported"] == 2

        resp = client.get("/api/pricing/vendor-lists?vendor=CHI")
        assert len(resp.json()) == 2

    def test_filter_by_vendor(self, client: TestClient) -> None:
        client.post("/api/pricing/vendor-lists", json={
            "items": [{"vendor_name": "Amarr", "sku": "AMR-100", "description": "Basic", "cost": 200.00}]
        })
        resp = client.get("/api/pricing/vendor-lists?vendor=Amarr")
        assert all(i["vendor_name"] == "Amarr" for i in resp.json())


class TestPriceComparison:
    def test_comparison_returns_margin(self, client: TestClient) -> None:
        client.post("/api/pricing/vendor-lists", json={
            "items": [{"vendor_name": "Test", "sku": "T-1", "description": "Test part", "cost": 100.00}]
        })
        resp = client.get("/api/pricing/comparison?customer_type=retail")
        assert resp.status_code == 200
        items = resp.json()
        for item in items:
            if item["sku"] == "T-1":
                assert item["sell_price"] > item["cost"]
                assert item["margin_pct"] > 0


class TestPriceBookVersioning:
    def test_lock_and_retrieve(self, client: TestClient) -> None:
        resp = client.post("/api/pricing/lock-prices", json={
            "estimate_id": "est-123",
            "line_items": [{"sku": "S-1", "price": 100.0, "qty": 2}],
        })
        assert resp.json()["locked"] is True

        resp = client.get("/api/pricing/locked/est-123")
        assert resp.status_code == 200
        assert resp.json()["estimate_id"] == "est-123"

    def test_404_for_missing(self, client: TestClient) -> None:
        resp = client.get("/api/pricing/locked/nonexistent")
        assert resp.status_code == 404


class TestSeasonalPricing:
    def test_set_and_get(self, client: TestClient) -> None:
        client.patch("/api/pricing/seasonal", json={
            "category": "doors", "season": "summer", "adjustment_pct": 0.05
        })
        resp = client.get("/api/pricing/seasonal")
        assert any(s["category"] == "doors" and s["season"] == "summer" for s in resp.json())


class TestBundlePricing:
    def test_create_and_calculate(self, client: TestClient) -> None:
        client.post("/api/pricing/bundles", json={
            "name": "Door + Install + Disposal",
            "items": [
                {"sku": "DOOR-1", "quantity": 1, "unit_price": 450.00},
                {"sku": "INSTALL", "quantity": 1, "unit_price": 200.00},
                {"sku": "DISPOSAL", "quantity": 1, "unit_price": 75.00},
            ],
            "bundle_discount_pct": 10,
        })

        resp = client.post("/api/pricing/bundles/calculate?name=Door+%2B+Install+%2B+Disposal")
        assert resp.status_code == 200
        data = resp.json()
        assert data["subtotal"] == 725.00
        assert data["total"] == 652.50  # 725 * 0.9


class TestCustomerRates:
    def test_set_and_get(self, client: TestClient) -> None:
        client.post("/api/pricing/customer-rates", json={
            "customer_id": "cust-1", "discount_pct": 10, "custom_labor_rate": 65.00
        })
        resp = client.get("/api/pricing/customer-rates/cust-1")
        assert resp.status_code == 200
        assert resp.json()["discount_pct"] == 10

    def test_404_for_missing(self, client: TestClient) -> None:
        resp = client.get("/api/pricing/customer-rates/nonexistent")
        assert resp.status_code == 404


class TestApprovalWorkflow:
    def test_set_rule_and_check(self, client: TestClient) -> None:
        client.post("/api/pricing/approval-rules", json={
            "threshold_amount": 5000, "required_role": "admin"
        })

        # Under threshold — no approval needed
        resp = client.post("/api/pricing/check-approval", json={
            "quote_amount": 3000, "user_role": "tech"
        })
        assert resp.json()["requires_approval"] is False

        # Over threshold, tech role — needs approval
        resp = client.post("/api/pricing/check-approval", json={
            "quote_amount": 6000, "user_role": "tech"
        })
        assert resp.json()["requires_approval"] is True

        # Over threshold, admin role — no approval needed
        resp = client.post("/api/pricing/check-approval", json={
            "quote_amount": 6000, "user_role": "admin"
        })
        assert resp.json()["requires_approval"] is False
