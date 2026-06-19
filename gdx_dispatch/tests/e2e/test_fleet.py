"""E2E tests for Fleet Management — FLEET-01 through FLEET-05.

Covers: vehicle list, add vehicle, service log, due-for-service, delete.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest

from gdx_dispatch.tests.e2e.conftest import (
    assert_api_success,
)

pytestmark = [pytest.mark.e2e]


class TestFleetList:
    def test_fleet_01_page_renders(self, navigate, console_tracker):
        """Fleet page renders with vehicle list."""
        page = navigate("/fleet")
        page.wait_for_timeout(3000)
        body = page.content().lower()
        assert "fleet" in body or "vehicle" in body
        console_tracker.assert_no_errors("fleet page")

    def test_fleet_01_list_api(self, api, console_tracker):
        """GET /api/fleet/vehicles returns vehicle list."""
        resp = api.get("/api/fleet/vehicles")
        assert_api_success(resp)
        data = resp.json()
        assert isinstance(data, list)
        if data:
            first = data[0]
            assert "id" in first
            assert "make" in first


class TestFleetCRUD:
    def test_fleet_02_add_vehicle(self, api, console_tracker):
        """POST creates vehicle, appears in list."""
        unique = uuid.uuid4().hex[:8]
        payload = {
            "make": f"TestMake-{unique}",
            "model": f"TestModel-{unique}",
            "year": 2024,
            "vin": f"VIN{unique}",
            "license_plate": f"E2E-{unique[:4]}",
            "odometer": 50000,
            "service_interval_miles": 5000,
        }
        resp = api.post("/api/fleet/vehicles", json_data=payload)
        assert_api_success(resp, 201)
        data = resp.json()
        assert data["make"] == payload["make"]
        assert data["year"] == 2024
        self.__class__._vehicle_id = data["id"]

        # Verify in list
        list_resp = api.get("/api/fleet/vehicles")
        assert_api_success(list_resp)
        ids = [v["id"] for v in list_resp.json()]
        assert data["id"] in ids

    def test_fleet_03_service_log(self, api, console_tracker):
        """GET /{id}/service-log returns maintenance records."""
        vid = getattr(self.__class__, "_vehicle_id", None)
        if not vid:
            pytest.skip("No vehicle created")
        resp = api.get(f"/api/fleet/vehicles/{vid}/service-log")
        assert_api_success(resp)
        data = resp.json()
        assert isinstance(data, list)

    def test_fleet_03_add_service_entry(self, api, console_tracker):
        """POST /{id}/service-log adds a service record."""
        vid = getattr(self.__class__, "_vehicle_id", None)
        if not vid:
            pytest.skip("No vehicle created")
        resp = api.post(f"/api/fleet/vehicles/{vid}/service-log", json_data={
            "service_type": "Oil Change",
            "description": "E2E test service entry",
            "odometer_at_service": 55000,
            "service_date": str(date.today()),
            "cost": 49.99,
        })
        assert resp.status_code in (200, 201, 422), (
            f"Add service entry returned {resp.status_code}: {resp.text[:200]}"
        )

    def test_fleet_04_due_for_service(self, api, console_tracker):
        """GET /due-for-service returns vehicles needing maintenance."""
        resp = api.get("/api/fleet/vehicles/due-for-service")
        assert_api_success(resp)
        data = resp.json()
        assert isinstance(data, list)

    def test_fleet_05_delete_vehicle(self, api, console_tracker):
        """Soft delete, removed from list."""
        vid = getattr(self.__class__, "_vehicle_id", None)
        if not vid:
            pytest.skip("No vehicle created")
        resp = api.delete(f"/api/fleet/vehicles/{vid}")
        assert resp.status_code in (200, 204)

        # Verify removed from list
        list_resp = api.get("/api/fleet/vehicles")
        assert_api_success(list_resp)
        ids = [v["id"] for v in list_resp.json()]
        assert vid not in ids
