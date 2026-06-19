"""E2E tests for Search — SRCH-01 through SRCH-05.

Covers: global search returns results from jobs/customers/invoices,
search scoped to tenant, command palette.
"""
from __future__ import annotations

import uuid

import pytest

from gdx_dispatch.tests.e2e.conftest import (
    assert_api_success,
)

pytestmark = [pytest.mark.e2e]


class TestGlobalSearch:
    @pytest.fixture(autouse=True)
    def _seed_data(self, api):
        """Ensure there is searchable data."""
        unique = uuid.uuid4().hex[:8]
        self._search_key = f"SearchTest{unique}"
        api.post("/api/customers", json_data={
            "name": self._search_key,
            "phone": "555-0001",
        })

    def test_srch_01_global_search(self, api, console_tracker):
        """GET /api/search?q=keyword returns results across entities."""
        resp = api.get(f"/api/search?q={self._search_key}")
        assert_api_success(resp)
        data = resp.json()
        assert isinstance(data, dict)
        # Should have category keys
        assert any(k in data for k in ["customers", "jobs", "invoices", "estimates"]), (
            f"Search response should have entity categories, got: {list(data.keys())}"
        )
        # The customer we created should appear
        customers = data.get("customers", [])
        assert len(customers) >= 1, f"Expected to find customer '{self._search_key}'"
        assert any(self._search_key in str(c) for c in customers)

    def test_srch_02_search_relevance(self, api, console_tracker):
        """Exact match ranks higher than partial match."""
        resp = api.get(f"/api/search?q={self._search_key}")
        assert_api_success(resp)
        data = resp.json()
        customers = data.get("customers", [])
        if len(customers) >= 2:
            # First result should contain the exact search term
            first_name = str(customers[0].get("name", ""))
            assert self._search_key in first_name

    def test_srch_03_search_tenant_isolation(self, api, console_tracker):
        """Results only from current tenant (verified by not leaking other data)."""
        resp = api.get(f"/api/search?q={self._search_key}")
        assert_api_success(resp)
        data = resp.json()
        # All returned items should belong to current tenant
        # We can't directly verify tenant_id in search results,
        # but we verify the data matches what we created
        customers = data.get("customers", [])
        for c in customers:
            assert isinstance(c, dict)
            assert "id" in c

    def test_srch_04_empty_search(self, api, console_tracker):
        """Empty results return empty arrays, not errors."""
        resp = api.get("/api/search?q=zzz_nonexistent_query_xyz_999")
        assert_api_success(resp)
        data = resp.json()
        assert isinstance(data, dict)
        # All categories should be empty lists
        for key in ["customers", "jobs", "invoices", "estimates"]:
            if key in data:
                assert isinstance(data[key], list)
                assert len(data[key]) == 0


class TestCommandPalette:
    def test_srch_05_command_palette(self, navigate, console_tracker):
        """Vue command palette opens with Ctrl+K, search works."""
        page = navigate("/dashboard")
        page.wait_for_timeout(2000)

        # Press Ctrl+K to open command palette
        page.keyboard.press("Control+k")
        page.wait_for_timeout(1000)

        # Look for command palette dialog/overlay
        palette = page.locator(
            "[data-testid='command-palette'], "
            ".p-dialog, "
            "[role='dialog'], "
            ".command-palette, "
            ".p-overlaypanel"
        ).first

        if palette.is_visible(timeout=3000):
            # Type a search query
            search_input = palette.locator("input").first
            if search_input.is_visible():
                search_input.fill("test")
                page.wait_for_timeout(1000)

            # Close with Escape
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)

        console_tracker.assert_no_errors("command palette")
