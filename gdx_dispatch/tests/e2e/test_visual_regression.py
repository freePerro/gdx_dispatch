"""Playwright visual regression tests for GDX.

Takes full-viewport screenshots of every major page after login and compares
them against baseline images stored in gdx_dispatch/tests/e2e/screenshots/.

How baselines work:
  - First run: Playwright creates baseline screenshots automatically (no
    comparison, tests pass).
  - Subsequent runs: each screenshot is compared pixel-by-pixel against the
    stored baseline.  Tests fail if the difference exceeds 5%.

Updating baselines after intentional UI changes:
    pytest gdx_dispatch/tests/e2e/test_visual_regression.py --update-snapshots

Environment variables (see conftest.py for defaults):
    GDX_BASE_URL, GDX_E2E_EMAIL, GDX_E2E_PASSWORD, GDX_TENANT_ID
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

# Every major page in the app.  Tuple of (path, human-readable name used for
# the baseline filename).
PAGES = [
    ("/dashboard", "dashboard"),
    ("/jobs", "jobs"),
    ("/customers", "customers"),
    ("/estimates", "estimates"),
    ("/billing", "billing"),
    ("/dispatch", "dispatch"),
    ("/reports", "reports"),
    ("/settings", "settings"),
    ("/timeclock", "timeclock"),
]


@pytest.mark.e2e
@pytest.mark.parametrize("path,name", PAGES, ids=[name for _, name in PAGES])
def test_visual_regression(
    authenticated_page: Page,
    navigate,
    path: str,
    name: str,
) -> None:
    """Screenshot every major page and compare against the stored baseline.

    Tolerance is set to 5% pixel difference so minor anti-aliasing or font-
    rendering variations across environments don't cause false positives.
    """
    page = navigate(path)

    # Give data, charts, and lazy-loaded components time to render.
    page.wait_for_timeout(3000)

    # Wait for network to settle (no pending XHR/fetch for 500ms).
    page.wait_for_load_state("networkidle")

    # Compare (or create baseline on first run).
    expect(page).to_have_screenshot(
        f"{name}.png",
        full_page=False,
        max_diff_pixel_ratio=0.05,  # 5 % tolerance
    )
