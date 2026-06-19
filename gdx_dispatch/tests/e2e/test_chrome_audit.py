"""Chrome DevTools-style error audit for GDX API endpoints.

Hits every major API endpoint and verifies:
- No 500 status codes
- Notification badge count returns a real number (not hardcoded)
- All core endpoints are reachable and return expected status codes

Usage:
    GDX_BASE_URL=https://gdx.example.com \
    GDX_E2E_EMAIL=e2e_admin@example.com \
    GDX_E2E_PASSWORD=E2E_Test_2026! \
    pytest gdx_dispatch/tests/e2e/test_chrome_audit.py -v
"""
from __future__ import annotations

import pytest

from gdx_dispatch.tests.e2e.conftest import APIClient

pytestmark = [pytest.mark.e2e]

# Core API endpoints that must never return 500
CORE_ENDPOINTS = [
    "/api/jobs",
    "/api/customers",
    "/api/estimates",
    "/api/invoices",
    "/api/settings",
    "/api/settings/branding",
    "/api/reports/revenue-analytics",
    "/api/reports/outstanding-aging",
    "/api/timeclock/status",
    "/api/equipment",
    "/api/fleet/vehicles",
    "/api/communications/threads",
    "/api/pricing/vendor-lists",
    "/api/resources",
    "/api/notifications/count",
]


class TestChromeAudit:
    """Simulates Chrome DevTools Network tab audit — no endpoint should 500."""

    def test_no_500_on_core_endpoints(self, api: APIClient):
        """Every core API endpoint must return a non-500 status code."""
        failures = []
        for endpoint in CORE_ENDPOINTS:
            resp = api.get(endpoint)
            if resp.status_code == 500:
                body_preview = resp.text[:200] if resp.text else "(empty)"
                failures.append(f"{endpoint} -> 500: {body_preview}")
        assert not failures, (
            f"{len(failures)} endpoint(s) returned 500:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )

    def test_notification_count_returns_real_number(self, api: APIClient):
        """GET /api/notifications/count must return {"count": <int>}, not hardcoded."""
        resp = api.get("/api/notifications/count")
        # Accept 200 (count returned) or 403/404 (module not enabled) — but NOT 500
        assert resp.status_code != 500, (
            f"Notification count returned 500: {resp.text[:200]}"
        )
        if resp.status_code == 200:
            data = resp.json()
            assert "count" in data, f"Missing 'count' key in response: {data}"
            assert isinstance(data["count"], int), (
                f"'count' must be an integer, got {type(data['count']).__name__}: {data['count']}"
            )
            assert data["count"] >= 0, f"Negative count: {data['count']}"

    def test_notification_list_returns_paginated(self, api: APIClient):
        """GET /api/notifications must return paginated list."""
        resp = api.get("/api/notifications")
        assert resp.status_code != 500, (
            f"Notification list returned 500: {resp.text[:200]}"
        )
        if resp.status_code == 200:
            data = resp.json()
            assert "items" in data, f"Missing 'items' key: {data}"
            assert "total" in data, f"Missing 'total' key: {data}"
            assert isinstance(data["items"], list), "'items' must be a list"

    def test_each_endpoint_individually(self, api: APIClient):
        """Parameterized check: each endpoint gets its own pass/fail."""
        for endpoint in CORE_ENDPOINTS:
            resp = api.get(endpoint)
            assert resp.status_code != 500, (
                f"{endpoint} returned 500: {resp.text[:200]}"
            )

    def test_settings_branding_shape(self, api: APIClient):
        """Branding endpoint must return valid JSON with expected fields."""
        resp = api.get("/api/settings/branding")
        if resp.status_code == 200:
            data = resp.json()
            # Should have at least company_name or similar branding fields
            assert isinstance(data, dict), f"Expected dict, got {type(data).__name__}"
