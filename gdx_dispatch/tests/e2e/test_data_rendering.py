"""E2E data rendering tests — verify API data actually appears in the rendered DOM.

Goes beyond status code checks: fetches data via API, then navigates the page
and asserts that specific values (customer names, invoice amounts, job descriptions)
appear in the browser. Also checks for rendering failures like stuck "Loading..."
spinners, "Unknown" placeholder text, and "undefined" leaking into the UI.

Covers cross-cutting rendering quality across all major pages.
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page

from gdx_dispatch.tests.e2e.conftest import (
    E2E_PASSWORD,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not E2E_PASSWORD, reason="GDX_E2E_PASSWORD not set"),
]

RENDER_WAIT = 3000
LONG_RENDER_WAIT = 5000

# Pages to check for cross-cutting rendering issues
MAJOR_PAGES = [
    "/dashboard",
    "/jobs",
    "/customers",
    "/billing",
    "/estimates",
    "/dispatch",
]


def _extract_items(resp_json) -> list:
    """Extract items list from various API response shapes."""
    if isinstance(resp_json, list):
        return resp_json
    if isinstance(resp_json, dict):
        return (
            resp_json.get("items")
            or resp_json.get("data")
            or resp_json.get("results")
            or []
        )
    return []


# ---------------------------------------------------------------------------
# API data appears in DOM
# ---------------------------------------------------------------------------
class TestAPIDataRenderedInDOM:
    """Fetch data from API, then verify it shows up on the page."""

    def test_customer_names_appear_in_table(
        self, navigate, api, authenticated_page: Page
    ):
        """Customer names from API must appear in the customers page."""
        resp = api.get("/api/customers")
        if resp.status_code != 200:
            pytest.skip(f"Cannot fetch customers (HTTP {resp.status_code})")

        customers = _extract_items(resp.json())
        if not customers:
            pytest.skip("No customers in database")

        page = navigate("/customers")
        page.wait_for_timeout(RENDER_WAIT)
        body_text = page.locator("body").inner_text()

        # At least the first customer name should appear on the page
        first_name = customers[0].get("name", "")
        if not first_name:
            pytest.skip("First customer has no name field")

        assert first_name in body_text, (
            f"Customer '{first_name}' not found in rendered page. "
            f"Page text starts with: '{body_text[:200]}...'"
        )

    def test_job_descriptions_appear_in_table(
        self, navigate, api, authenticated_page: Page
    ):
        """Job descriptions/titles from API must appear in the jobs page."""
        resp = api.get("/api/jobs")
        if resp.status_code != 200:
            pytest.skip(f"Cannot fetch jobs (HTTP {resp.status_code})")

        jobs = _extract_items(resp.json())
        if not jobs:
            pytest.skip("No jobs in database")

        page = navigate("/jobs")
        page.wait_for_timeout(RENDER_WAIT)
        body_text = page.locator("body").inner_text()

        # Check for the first job's description or title
        first_desc = (
            jobs[0].get("description")
            or jobs[0].get("title")
            or jobs[0].get("summary")
            or ""
        )
        if not first_desc:
            # Fall back to checking that customer_name appears
            first_desc = jobs[0].get("customer_name", "")
        if not first_desc:
            pytest.skip("First job has no description/title/customer_name field")

        assert first_desc in body_text, (
            f"Job data '{first_desc}' not found in rendered page"
        )

    def test_estimate_data_appears_in_table(
        self, navigate, api, authenticated_page: Page
    ):
        """Estimate amounts or customer names from API must appear on the estimates page."""
        resp = api.get("/api/estimates")
        if resp.status_code != 200:
            pytest.skip(f"Cannot fetch estimates (HTTP {resp.status_code})")

        estimates = _extract_items(resp.json())
        if not estimates:
            pytest.skip("No estimates in database")

        page = navigate("/estimates")
        page.wait_for_timeout(RENDER_WAIT)
        body_text = page.locator("body").inner_text()

        # Look for a customer name or an amount
        first = estimates[0]
        search_value = (
            first.get("customer_name")
            or first.get("name")
            or ""
        )
        if not search_value:
            # Try matching a dollar amount
            total = first.get("total") or first.get("amount")
            if total is not None:
                # Format as string — page may show "1,234.56" or "1234.56"
                search_value = f"{float(total):.2f}"

        if not search_value:
            pytest.skip("First estimate has no identifiable display value")

        assert search_value in body_text, (
            f"Estimate data '{search_value}' not found in rendered page"
        )

    def test_invoice_amounts_appear_in_billing(
        self, navigate, api, authenticated_page: Page
    ):
        """Invoice amounts from API must appear in the billing page."""
        resp = api.get("/api/invoices")
        if resp.status_code != 200:
            pytest.skip(f"Cannot fetch invoices (HTTP {resp.status_code})")

        invoices = _extract_items(resp.json())
        if not invoices:
            pytest.skip("No invoices in database")

        page = navigate("/billing")
        page.wait_for_timeout(RENDER_WAIT)
        body_text = page.locator("body").inner_text()

        first = invoices[0]
        search_value = (
            first.get("customer_name")
            or first.get("name")
            or ""
        )
        if not search_value:
            total = first.get("total") or first.get("amount")
            if total is not None:
                search_value = f"{float(total):.2f}"

        if not search_value:
            pytest.skip("First invoice has no identifiable display value")

        assert search_value in body_text, (
            f"Invoice data '{search_value}' not found in billing page"
        )


# ---------------------------------------------------------------------------
# Rendering quality — cross-cutting checks
# ---------------------------------------------------------------------------
class TestNoRenderingFailures:
    """Detect stuck spinners, placeholder text, and rendering artifacts."""

    def test_no_loading_forever(
        self, navigate, authenticated_page: Page
    ):
        """No page should still show 'Loading...' after 5 seconds."""
        stuck_pages = []
        for path in MAJOR_PAGES:
            page = navigate(path)
            page.wait_for_timeout(LONG_RENDER_WAIT)

            # Check for loading indicators — text or spinners
            body_text = page.locator("body").inner_text().lower()
            # Match "loading..." but not "loading dock" or "loading bay" (real data)
            if re.search(r"\bloading\.{3}\b|\bloading\.\.\.\b", body_text):
                stuck_pages.append(path)

        assert not stuck_pages, (
            f"Pages still showing 'Loading...' after {LONG_RENDER_WAIT}ms: "
            + ", ".join(stuck_pages)
        )

    def test_no_undefined_in_dom(
        self, navigate, authenticated_page: Page
    ):
        """Pages should not display literal 'undefined' as data values."""
        pages_with_undefined = []
        for path in MAJOR_PAGES:
            page = navigate(path)
            page.wait_for_timeout(RENDER_WAIT)

            body_text = page.locator("body").inner_text()
            # Look for 'undefined' as a standalone word (not part of another word)
            matches = re.findall(r"\bundefined\b", body_text, re.IGNORECASE)
            if matches:
                pages_with_undefined.append(f"{path} ({len(matches)}x)")

        assert not pages_with_undefined, (
            "Pages showing literal 'undefined': "
            + ", ".join(pages_with_undefined)
        )

    def test_no_null_in_dom(
        self, navigate, authenticated_page: Page
    ):
        """Pages should not display literal 'null' as data values."""
        pages_with_null = []
        for path in MAJOR_PAGES:
            page = navigate(path)
            page.wait_for_timeout(RENDER_WAIT)

            body_text = page.locator("body").inner_text()
            # Match 'null' as a standalone visible value, not in code/attribute context
            matches = re.findall(r"\bnull\b", body_text)
            if matches:
                pages_with_null.append(f"{path} ({len(matches)}x)")

        assert not pages_with_null, (
            "Pages showing literal 'null': "
            + ", ".join(pages_with_null)
        )

    def test_no_unknown_placeholders(
        self, navigate, authenticated_page: Page
    ):
        """Pages should not show 'Unknown' as a data value in tables."""
        pages_with_unknown = []
        for path in ["/jobs", "/customers", "/billing", "/estimates"]:
            page = navigate(path)
            page.wait_for_timeout(RENDER_WAIT)

            # Only check table/card areas, not headings or labels
            table_area = page.locator(
                "table, .p-datatable, [data-testid*='list'], "
                "[data-testid*='table'], main"
            ).first
            if not table_area.count():
                continue

            text = table_area.inner_text()
            unknown_count = text.lower().count("unknown")
            if unknown_count > 0:
                pages_with_unknown.append(f"{path} ({unknown_count}x)")

        assert not pages_with_unknown, (
            "Pages showing 'Unknown' in data areas: "
            + ", ".join(pages_with_unknown)
        )

    def test_no_empty_table_cells(
        self, navigate, api, authenticated_page: Page
    ):
        """Table cells in data-rich pages should not be mostly empty."""
        for path, api_path in [
            ("/jobs", "/api/jobs"),
            ("/customers", "/api/customers"),
        ]:
            resp = api.get(api_path)
            if resp.status_code != 200:
                continue
            items = _extract_items(resp.json())
            if not items:
                continue

            page = navigate(path)
            page.wait_for_timeout(RENDER_WAIT)

            cells = page.locator("table tbody td, .p-datatable-tbody td").all()
            if not cells:
                continue

            visible_cells = [c for c in cells if c.is_visible()]
            if not visible_cells:
                continue

            empty_count = sum(
                1 for c in visible_cells if len(c.inner_text().strip()) == 0
            )
            empty_ratio = empty_count / len(visible_cells)
            assert empty_ratio < 0.5, (
                f"{path}: {empty_count}/{len(visible_cells)} table cells are empty "
                f"({empty_ratio:.0%}) — data is not rendering into the table"
            )


# ---------------------------------------------------------------------------
# Row count consistency — API count vs DOM count
# ---------------------------------------------------------------------------
class TestRowCountConsistency:
    """Verify the number of rows in the DOM roughly matches the API response."""

    @pytest.mark.parametrize(
        "page_path, api_path",
        [
            ("/jobs", "/api/jobs"),
            ("/customers", "/api/customers"),
            ("/estimates", "/api/estimates"),
        ],
    )
    def test_row_count_matches_api(
        self, navigate, api, authenticated_page: Page, page_path, api_path
    ):
        """DOM row count should be within range of API item count."""
        resp = api.get(api_path)
        if resp.status_code != 200:
            pytest.skip(f"{api_path} returned {resp.status_code}")

        items = _extract_items(resp.json())
        api_count = len(items)
        if api_count == 0:
            pytest.skip(f"No items from {api_path}")

        page = navigate(page_path)
        page.wait_for_timeout(RENDER_WAIT)

        rows = page.locator(
            "table tbody tr, .p-datatable-tbody tr, [data-testid*='-row']"
        ).all()
        dom_count = len(rows)

        # Allow for pagination — DOM might show fewer rows than API total,
        # but should show at least 1 row if API has data
        assert dom_count > 0, (
            f"{page_path}: API has {api_count} items but DOM has 0 rows"
        )

        # If API returns <= 25 items (typical page size), counts should be close
        if api_count <= 25:
            assert dom_count <= api_count + 2, (
                f"{page_path}: DOM has {dom_count} rows but API only has {api_count} items"
            )
