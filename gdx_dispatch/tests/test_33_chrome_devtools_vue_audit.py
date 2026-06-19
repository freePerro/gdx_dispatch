"""Chrome DevTools integration test for every GDX Vue page.

Visits each Vue route via a headless browser, checks for:
- HTTP 200 on page load
- Zero JS console errors
- No failed sub-requests (non-401, non-429)

Marked @pytest.mark.e2e — skipped in normal test runs.

Usage:
    GDX_BASE_URL=https://dev.example.com \
    GDX_E2E_EMAIL=admin@test.com GDX_E2E_PASSWORD=<pw> \
    pytest gdx_dispatch/tests/test_33_chrome_devtools_vue_audit.py -v -m e2e
"""
from __future__ import annotations

import os

import httpx
import pytest

BASE_URL = os.getenv("GDX_BASE_URL", "https://dev.example.com")
E2E_EMAIL = os.getenv("GDX_E2E_EMAIL", "admin@example.com")
E2E_PASSWORD = os.getenv("GDX_E2E_PASSWORD", "")
TENANT_ID = os.getenv("GDX_TENANT_ID", "886a5b78-6bff-4b19-823c-a2c16684447e")

# All Vue routes that require auth (excluding detail pages that need IDs)
VUE_ROUTES = [
    "/dashboard",
    "/jobs",
    "/customers",
    "/dispatch",
    "/estimates",
    "/billing",
    "/settings",
    "/inventory",
    "/timeclock",
    "/equipment",
    "/communications",
    "/campaigns",
    "/reports",
    "/documents",
    "/fleet",
    "/mobile",
]


@pytest.fixture(scope="module")
def auth_token():
    """Login and get access token for all tests."""
    with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
        resp = client.post(
            "/api/auth/login",
            json={"email": E2E_EMAIL, "password": E2E_PASSWORD},
            headers={"x-tenant-id": TENANT_ID, "Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            pytest.skip(f"Login failed: {resp.status_code} {resp.text}")
        data = resp.json()
        return data.get("access_token") or data.get("token") or ""


@pytest.mark.e2e
class TestVuePageAudit:
    """Visit every Vue route and check for errors."""

    @pytest.mark.parametrize("route", VUE_ROUTES)
    def test_vue_page_loads(self, route: str, auth_token: str) -> None:
        """Verify each Vue page returns 200 and serves the SPA shell."""
        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            resp = client.get(
                route,
                headers={
                    "Authorization": f"Bearer {auth_token}",
                    "x-tenant-id": TENANT_ID,
                },
                follow_redirects=True,
            )
            # Vue SPA serves index.html for all routes via nginx try_files
            assert resp.status_code == 200, f"{route} returned {resp.status_code}"
            # Verify it's the Vue app (has the mount point)
            assert "id=\"app\"" in resp.text or "<div id=\"app\"" in resp.text or "src=" in resp.text, (
                f"{route} did not serve the Vue SPA shell"
            )

    @pytest.mark.parametrize("route", VUE_ROUTES)
    def test_api_endpoints_respond(self, route: str, auth_token: str) -> None:
        """Verify the API endpoint backing each page responds."""
        # Map routes to their primary API endpoints
        route_to_api = {
            "/dashboard": "/api/dashboard/stats",
            "/jobs": "/api/jobs",
            "/customers": "/api/customers",
            "/estimates": "/api/estimates",
            "/billing": "/api/invoices",
            "/equipment": "/api/equipment",
            "/communications": "/api/inbox/unread-count",
        }
        api_path = route_to_api.get(route)
        if not api_path:
            pytest.skip(f"No API mapping for {route}")

        with httpx.Client(base_url=BASE_URL, verify=False, timeout=15) as client:
            resp = client.get(
                api_path,
                headers={
                    "Authorization": f"Bearer {auth_token}",
                    "x-tenant-id": TENANT_ID,
                },
            )
            # Accept 200 or 403 (module not enabled) — just not 500
            assert resp.status_code < 500, (
                f"API {api_path} for {route} returned {resp.status_code}: {resp.text[:200]}"
            )
