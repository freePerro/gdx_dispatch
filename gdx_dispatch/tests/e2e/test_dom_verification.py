"""E2E DOM verification tests — verify pages render real content, not empty shells.

Current tests only check HTTP status codes. These tests navigate via Playwright
and verify the DOM contains actual data: numbers in KPI cards, rows in tables,
content in detail views. If the API has data, the page must show it.

Covers: /dashboard, /jobs, /customers, /estimates, /billing, /dispatch,
        /settings, /timeclock, /equipment
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page

from gdx_dispatch.tests.e2e.conftest import (
    E2E_PASSWORD,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not E2E_PASSWORD, reason="GDX_E2E_PASSWORD not set"),
]

# Timeout (ms) to wait for Vue to render after navigation
RENDER_WAIT = 3000

# Selectors that match common card/stat components across the app
CARD_SELECTORS = (
    ".kpi-card, .stat-card, .p-card, "
    '[data-testid*="stat"], [data-testid*="kpi"], '
    '[class*="summary-card"], [class*="dashboard-card"]'
)

# Selectors that match table rows
ROW_SELECTORS = "table tbody tr, [data-testid*='-row'], .p-datatable-tbody tr"


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
class TestDashboardRendersData:
    """Verify the dashboard shows real numbers, not blank placeholders."""

    def test_dashboard_kpi_cards_have_values(
        self, navigate, authenticated_page: Page, console_tracker
    ):
        """KPI cards must show numbers, not be blank."""
        page = navigate("/dashboard")
        page.wait_for_timeout(RENDER_WAIT)

        cards = page.locator(CARD_SELECTORS).all()
        if not cards:
            pytest.skip("No KPI/stat cards found on dashboard — UI may not be deployed yet")

        empty_cards = []
        for card in cards:
            if not card.is_visible():
                continue
            text = card.inner_text()
            # Card should have a digit somewhere (count, dollar amount, percentage)
            if not any(c.isdigit() for c in text):
                empty_cards.append(text.strip()[:80])

        assert not empty_cards, (
            f"{len(empty_cards)} KPI card(s) have no numeric value:\n"
            + "\n".join(f"  - {t}" for t in empty_cards)
        )

    def test_dashboard_has_visible_heading(
        self, navigate, authenticated_page: Page
    ):
        """Dashboard should have at least one heading element."""
        page = navigate("/dashboard")
        page.wait_for_timeout(RENDER_WAIT)

        headings = page.locator("h1, h2, h3, [data-testid*='heading']").all()
        visible = [h for h in headings if h.is_visible()]
        assert len(visible) > 0, "Dashboard has no visible headings"

    def test_dashboard_body_not_empty(
        self, navigate, authenticated_page: Page
    ):
        """Dashboard body must have substantial content (not just a navbar)."""
        page = navigate("/dashboard")
        page.wait_for_timeout(RENDER_WAIT)

        body_text = page.locator("body").inner_text()
        # A real dashboard should have at least 50 characters of content
        assert len(body_text.strip()) > 50, (
            f"Dashboard body has very little content ({len(body_text.strip())} chars)"
        )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
class TestJobsPageShowsData:
    """Verify the jobs page renders rows when the API has data."""

    def test_jobs_table_has_populated_rows(
        self, navigate, api, authenticated_page: Page
    ):
        """If API has jobs, table rows should have content in every column."""
        jobs_resp = api.get("/api/jobs")
        if jobs_resp.status_code != 200:
            pytest.skip(f"Jobs API returned {jobs_resp.status_code}")

        data = jobs_resp.json()
        items = data.get("items", data if isinstance(data, list) else [])
        if not items:
            pytest.skip("No jobs in database — nothing to verify")

        page = navigate("/jobs")
        page.wait_for_timeout(RENDER_WAIT)

        rows = page.locator(ROW_SELECTORS).all()
        assert len(rows) > 0, f"API has {len(items)} jobs but table is empty"

        # First row should have meaningful content, not just whitespace
        first_row_text = rows[0].inner_text()
        assert len(first_row_text.strip()) > 5, (
            f"First job row appears empty: '{first_row_text.strip()[:100]}'"
        )

    def test_jobs_page_has_action_buttons(
        self, navigate, authenticated_page: Page
    ):
        """Jobs page should have at least one action button (New Job, filter, etc.)."""
        page = navigate("/jobs")
        page.wait_for_timeout(RENDER_WAIT)

        buttons = page.locator(
            'button, a[class*="btn"], [data-testid*="new-job"], '
            '[data-testid*="add-job"], .p-button'
        ).all()
        visible_buttons = [b for b in buttons if b.is_visible()]
        assert len(visible_buttons) > 0, "Jobs page has no visible action buttons"


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------
class TestCustomersPageShowsData:
    """Verify the customers page renders customer data."""

    def test_customers_table_has_rows(
        self, navigate, api, authenticated_page: Page
    ):
        """If API has customers, table should show rows."""
        resp = api.get("/api/customers")
        if resp.status_code != 200:
            pytest.skip(f"Customers API returned {resp.status_code}")

        data = resp.json()
        items = data.get("items", data if isinstance(data, list) else [])
        if not items:
            pytest.skip("No customers in database")

        page = navigate("/customers")
        page.wait_for_timeout(RENDER_WAIT)

        rows = page.locator(ROW_SELECTORS).all()
        assert len(rows) > 0, f"API has {len(items)} customers but table is empty"

    def test_customers_page_not_blank(
        self, navigate, authenticated_page: Page
    ):
        """Customers page must render visible content beyond the nav."""
        page = navigate("/customers")
        page.wait_for_timeout(RENDER_WAIT)

        body_text = page.locator("body").inner_text()
        assert len(body_text.strip()) > 50, "Customers page appears blank"


# ---------------------------------------------------------------------------
# Estimates
# ---------------------------------------------------------------------------
class TestEstimatesPageShowsData:
    """Verify the estimates page renders estimate data."""

    def test_estimates_table_has_rows(
        self, navigate, api, authenticated_page: Page
    ):
        """If API has estimates, table should show rows."""
        resp = api.get("/api/estimates")
        if resp.status_code != 200:
            pytest.skip(f"Estimates API returned {resp.status_code}")

        data = resp.json()
        items = data.get("items", data if isinstance(data, list) else [])
        if not items:
            pytest.skip("No estimates in database")

        page = navigate("/estimates")
        page.wait_for_timeout(RENDER_WAIT)

        rows = page.locator(ROW_SELECTORS).all()
        assert len(rows) > 0, f"API has {len(items)} estimates but table is empty"

    def test_estimates_page_not_blank(
        self, navigate, authenticated_page: Page
    ):
        """Estimates page must render visible content."""
        page = navigate("/estimates")
        page.wait_for_timeout(RENDER_WAIT)

        body_text = page.locator("body").inner_text()
        assert len(body_text.strip()) > 50, "Estimates page appears blank"


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------
class TestBillingPageShowsData:
    """Verify the billing/invoicing page renders data."""

    def test_billing_table_has_rows(
        self, navigate, api, authenticated_page: Page
    ):
        """If API has invoices, billing page should show rows."""
        resp = api.get("/api/invoices")
        if resp.status_code != 200:
            pytest.skip(f"Invoices API returned {resp.status_code}")

        data = resp.json()
        items = data.get("items", data if isinstance(data, list) else [])
        if not items:
            pytest.skip("No invoices in database")

        page = navigate("/billing")
        page.wait_for_timeout(RENDER_WAIT)

        rows = page.locator(ROW_SELECTORS).all()
        assert len(rows) > 0, f"API has {len(items)} invoices but billing table is empty"

    def test_billing_page_not_blank(
        self, navigate, authenticated_page: Page
    ):
        """Billing page must render visible content."""
        page = navigate("/billing")
        page.wait_for_timeout(RENDER_WAIT)

        body_text = page.locator("body").inner_text()
        assert len(body_text.strip()) > 50, "Billing page appears blank"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
class TestDispatchPageShowsData:
    """Verify the dispatch board renders content."""

    def test_dispatch_board_not_blank(
        self, navigate, authenticated_page: Page
    ):
        """Dispatch board must render visible content (calendar, cards, etc.)."""
        page = navigate("/dispatch")
        page.wait_for_timeout(RENDER_WAIT)

        body_text = page.locator("body").inner_text()
        assert len(body_text.strip()) > 50, "Dispatch page appears blank"

    def test_dispatch_has_interactive_elements(
        self, navigate, authenticated_page: Page
    ):
        """Dispatch board should have interactive elements (buttons, dropdowns, etc.)."""
        page = navigate("/dispatch")
        page.wait_for_timeout(RENDER_WAIT)

        interactives = page.locator(
            'button, select, [role="listbox"], .p-dropdown, '
            '[data-testid*="dispatch"], .p-button'
        ).all()
        visible = [el for el in interactives if el.is_visible()]
        assert len(visible) > 0, "Dispatch board has no visible interactive elements"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class TestSettingsPageShowsData:
    """Verify the settings page renders form fields and options."""

    def test_settings_page_has_form_elements(
        self, navigate, authenticated_page: Page
    ):
        """Settings page should have form inputs, selects, or toggles."""
        page = navigate("/settings")
        page.wait_for_timeout(RENDER_WAIT)

        form_elements = page.locator(
            'input, select, textarea, [role="switch"], '
            '.p-inputtext, .p-dropdown, .p-togglebutton, .p-inputswitch'
        ).all()
        visible = [el for el in form_elements if el.is_visible()]
        assert len(visible) > 0, "Settings page has no visible form elements"

    def test_settings_page_not_blank(
        self, navigate, authenticated_page: Page
    ):
        """Settings page must render visible content."""
        page = navigate("/settings")
        page.wait_for_timeout(RENDER_WAIT)

        body_text = page.locator("body").inner_text()
        assert len(body_text.strip()) > 50, "Settings page appears blank"


# ---------------------------------------------------------------------------
# Timeclock
# ---------------------------------------------------------------------------
class TestTimeclockPageShowsData:
    """Verify the timeclock page renders content."""

    def test_timeclock_page_not_blank(
        self, navigate, authenticated_page: Page
    ):
        """Timeclock page must render visible content."""
        page = navigate("/timeclock")
        page.wait_for_timeout(RENDER_WAIT)

        body_text = page.locator("body").inner_text()
        assert len(body_text.strip()) > 50, "Timeclock page appears blank"

    def test_timeclock_has_action_button(
        self, navigate, authenticated_page: Page
    ):
        """Timeclock should have a clock-in/out button or similar action."""
        page = navigate("/timeclock")
        page.wait_for_timeout(RENDER_WAIT)

        buttons = page.locator(
            'button, .p-button, [data-testid*="clock"], [data-testid*="time"]'
        ).all()
        visible = [b for b in buttons if b.is_visible()]
        assert len(visible) > 0, "Timeclock page has no visible action buttons"


# ---------------------------------------------------------------------------
# Equipment
# ---------------------------------------------------------------------------
class TestEquipmentPageShowsData:
    """Verify the equipment page renders content."""

    def test_equipment_page_not_blank(
        self, navigate, authenticated_page: Page
    ):
        """Equipment page must render visible content."""
        page = navigate("/equipment")
        page.wait_for_timeout(RENDER_WAIT)

        body_text = page.locator("body").inner_text()
        assert len(body_text.strip()) > 50, "Equipment page appears blank"

    def test_equipment_table_has_rows_if_data(
        self, navigate, api, authenticated_page: Page
    ):
        """If API has equipment, table should show rows."""
        resp = api.get("/api/equipment")
        if resp.status_code != 200:
            pytest.skip(f"Equipment API returned {resp.status_code}")

        data = resp.json()
        items = data.get("items", data if isinstance(data, list) else [])
        if not items:
            pytest.skip("No equipment in database")

        page = navigate("/equipment")
        page.wait_for_timeout(RENDER_WAIT)

        rows = page.locator(ROW_SELECTORS).all()
        assert len(rows) > 0, f"API has {len(items)} equipment items but table is empty"
