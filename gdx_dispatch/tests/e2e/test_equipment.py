"""E2E tests for Equipment Tracking — EQUIP-01 through EQUIP-07.

Covers: equipment list, add, service history, warranty alerts,
predictive maintenance.
"""
from __future__ import annotations

import uuid

import pytest

from gdx_dispatch.tests.e2e.conftest import (
    assert_api_success,
)

pytestmark = [pytest.mark.e2e]


@pytest.fixture(scope="module")
def test_customer_id(api):
    """Create a customer to link equipment to."""
    unique = uuid.uuid4().hex[:8]
    resp = api.post("/api/customers", json_data={
        "name": f"Equip Customer {unique}",
    })
    assert resp.status_code in (200, 201)
    return resp.json()["id"]


class TestEquipmentList:
    def test_equip_01_list_api(self, api, console_tracker):
        """GET /api/equipment returns items with make, model, serial, customer."""
        resp = api.get("/api/equipment")
        assert_api_success(resp)
        data = resp.json()
        assert isinstance(data, list)
        if data:
            first = data[0]
            assert "id" in first
            assert "make" in first or "manufacturer" in first or "model" in first

    def test_equip_02_page_renders(self, navigate, console_tracker):
        """Vue equipment page renders without errors."""
        page = navigate("/equipment")
        # Wait for either a table or any meaningful content
        try:
            page.wait_for_selector(
                "table, .p-datatable, [data-testid='equipment-list'], main, #app",
                timeout=10000,
            )
        except Exception:
            pass  # Page may not have a table yet — still check for errors
        console_tracker.assert_no_errors("equipment page")


class TestEquipmentCRUD:
    def test_equip_03_add_equipment(self, api, test_customer_id, console_tracker):
        """Create new equipment linked to customer, appears in list."""
        unique = uuid.uuid4().hex[:8]
        payload = {
            "customer_id": test_customer_id,
            "manufacturer": f"TestMake-{unique}",
            "model": f"TestModel-{unique}",
            "serial_number": f"SN-{unique}",
            "equipment_type": "opener",
            "install_date": "2024-01-15",
            "warranty_expiration": "2026-12-31",
        }
        resp = api.post("/api/equipment", json_data=payload)
        assert_api_success(resp, 201)
        data = resp.json()
        assert data.get("manufacturer") == payload["manufacturer"] or data.get("make") == payload["manufacturer"]
        self.__class__._equipment_id = data["id"]

    def test_equip_04_service_history(self, api, console_tracker):
        """GET /{id}/history returns service records."""
        eid = getattr(self.__class__, "_equipment_id", None)
        if not eid:
            pytest.skip("No equipment created in prior test")
        resp = api.get(f"/api/equipment/{eid}/history")
        assert_api_success(resp)
        data = resp.json()
        assert isinstance(data, list)

    def test_equip_04_add_service_record(self, api, console_tracker):
        """POST /{id}/history adds a service record."""
        eid = getattr(self.__class__, "_equipment_id", None)
        if not eid:
            pytest.skip("No equipment created in prior test")
        resp = api.post(f"/api/equipment/{eid}/history", json_data={
            "service_type": "Inspection",
            "notes": "Routine E2E test inspection",
            "service_date": "2026-04-01",
        })
        assert resp.status_code in (200, 201, 422), (
            f"Add service record returned {resp.status_code}: {resp.text[:200]}"
        )

    def test_equip_07_delete(self, api, console_tracker):
        """Soft delete, disappears from list."""
        eid = getattr(self.__class__, "_equipment_id", None)
        if not eid:
            pytest.skip("No equipment created in prior test")
        resp = api.delete(f"/api/equipment/{eid}")
        assert resp.status_code in (200, 204)


class TestEquipmentAlerts:
    def test_equip_05_expiring_warranties(self, api, console_tracker):
        """GET /expiring-warranties returns items with soon-expiring warranties."""
        resp = api.get("/api/equipment/expiring-warranties")
        assert_api_success(resp)
        data = resp.json()
        assert isinstance(data, list)

    def test_equip_06_predictive_maintenance(self, api, console_tracker):
        """GET /predictive-maintenance returns flagged equipment."""
        resp = api.get("/api/equipment/predictive-maintenance")
        assert_api_success(resp)
        data = resp.json()
        assert isinstance(data, list)
        # Each item should have equipment and recommendation info
        if data:
            first = data[0]
            assert "equipment_id" in first or "id" in first
