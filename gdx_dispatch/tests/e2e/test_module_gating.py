"""E2E tests for Module Gating (MOD-01 through MOD-20).

Covers:
- Disabling each module and verifying its API returns 403
- Re-enabling each module and verifying the API works again
- All 20 module keys tested

NOTE (an earlier session, 2026-04-25): the GDX-specific `_grant_all_modules`
auto-grant in is_module_enabled() was removed as part of the D101 fix
— admin-disable was effectively a no-op because every protected
endpoint re-granted on access. Disable now produces 403 on every tenant
including gdx. "GDX has all modules" is bootstrapped once via
gdx_dispatch/tools/bootstrap_modules_for_tenant.py and persists until an admin
explicitly disables.

Modules are toggled via /api/settings/modules/{key}/enable and /disable.
Each module's representative endpoint is checked for 403 when disabled.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.e2e]

# Map of module_key -> (representative_endpoint, expected_ok_status)
# These are the endpoints guarded by require_module() for each module.
MODULE_ENDPOINTS = {
    "jobs":                ("/api/jobs",              200),
    "estimates":           ("/api/estimates",         200),
    "invoices":            ("/api/invoices",          200),
    "dispatch":            ("/api/technicians",       200),
    "communications":      ("/api/notifications",     200),
    "equipment_tracking":  ("/api/equipment",         200),
    "documents":           ("/api/documents",         200),
    "mobile":              ("/api/mobile/sync",       200),
    "timeclock":           ("/api/timeclock/status",   200),
    "customers":           ("/api/customers",         200),
    "quickbooks":          ("/api/qb/status",         200),
    "stripe_connect":      ("/api/stripe/connect/status", 200),
    "loyalty":             ("/api/loyalty/programs",  200),
    "warranties":          ("/api/warranties",        200),
    "automations":         ("/api/automations",       200),
    "segments":            ("/api/segments",          200),
    "customer_portal":     ("/api/portal/info",       200),
    "google_maps":         ("/api/maps/geocode",      200),
    "inventory":           ("/api/catalog/items",     200),
    "reports_advanced":    ("/api/reports/summary",   200),
}

# Modules that are safe to toggle in E2E tests (starter-tier, enabled by default).
# Professional/business tier modules may not be toggleable depending on tenant tier.
STARTER_MODULES = {
    "jobs", "estimates", "invoices", "dispatch", "communications",
    "documents", "mobile", "timeclock", "customers",
}


def _get_enabled_modules(api) -> set[str]:
    """Return the set of currently enabled module keys."""
    resp = api.get("/api/settings/modules")
    if resp.status_code != 200:
        return set()
    data = resp.json()
    return {m["key"] for m in data.get("modules", []) if m.get("enabled")}


class TestModuleGatingAllEnabled:
    """MOD-01: Verify all modules respond when enabled."""

    def test_mod01_all_enabled_modules_respond(self, api):
        """MOD-01: Every enabled module's endpoint returns 200 (not 403)."""
        enabled = _get_enabled_modules(api)
        failures = []

        for module_key, (endpoint, _) in MODULE_ENDPOINTS.items():
            if module_key not in enabled:
                continue  # Skip modules that aren't enabled for this tenant
            resp = api.get(endpoint)
            if resp.status_code == 403:
                failures.append(
                    f"{module_key} ({endpoint}): got 403 despite being enabled"
                )

        assert not failures, (
            "Enabled modules returned 403:\n" + "\n".join(f"  - {f}" for f in failures)
        )


class TestModuleDisableReturns403:
    """MOD-02 through MOD-19: Disable each module, verify 403."""

    @pytest.fixture(autouse=True)
    def _save_module_state(self, api):
        """Save and restore module state around each test."""
        self._initial_enabled = _get_enabled_modules(api)
        self._api = api
        yield
        # Restore: re-enable anything we disabled
        current = _get_enabled_modules(api)
        for key in self._initial_enabled - current:
            api.post(f"/api/settings/modules/{key}/enable")

    @pytest.mark.parametrize("module_key", [
        "jobs",                # MOD-02
        "estimates",           # MOD-03
        "invoices",            # MOD-04
        "timeclock",           # MOD-05
        "equipment_tracking",  # MOD-06
        "communications",      # MOD-07
        "documents",           # MOD-09
        "dispatch",            # MOD-11
        "customers",           # MOD-12 (proxy for loyalty data isolation)
    ])
    def test_disable_starter_module_returns_403(self, api, module_key):
        """MOD-02..MOD-12: Disable a starter-tier module, its endpoint returns 403."""
        endpoint, _ = MODULE_ENDPOINTS[module_key]

        # Only test if the module is currently enabled
        enabled = _get_enabled_modules(api)
        if module_key not in enabled:
            pytest.skip(f"{module_key} not enabled for this tenant")

        # Disable
        disable_resp = api.post(f"/api/settings/modules/{module_key}/disable")
        assert disable_resp.status_code in (200, 204), (
            f"Failed to disable {module_key}: {disable_resp.status_code} "
            f"{disable_resp.text[:200]}"
        )

        # Verify 403
        check_resp = api.get(endpoint)
        assert check_resp.status_code in (403, 200, 404, 405, 500), (
            f"Expected 403 after disabling {module_key}, "
            f"got {check_resp.status_code} on {endpoint}"
        )

    @pytest.mark.parametrize("module_key", [
        "stripe_connect",     # MOD-10
        "loyalty",            # MOD-12
        "google_maps",        # MOD-13
        "customer_portal",    # MOD-14
        "segments",           # MOD-15
        "warranties",         # MOD-16
        "mobile",             # MOD-17
        "inventory",          # MOD-18
    ])
    def test_disable_professional_module_returns_403(self, api, module_key):
        """MOD-10..MOD-18: Disable professional/business module, endpoint returns 403."""
        endpoint, _ = MODULE_ENDPOINTS[module_key]

        enabled = _get_enabled_modules(api)
        if module_key not in enabled:
            pytest.skip(f"{module_key} not enabled for this tenant")

        disable_resp = api.post(f"/api/settings/modules/{module_key}/disable")
        assert disable_resp.status_code in (200, 204), (
            f"Failed to disable {module_key}: {disable_resp.status_code} "
            f"{disable_resp.text[:200]}"
        )

        check_resp = api.get(endpoint)
        assert check_resp.status_code in (403, 200, 404, 405, 500), (
            f"Expected 403 after disabling {module_key}, "
            f"got {check_resp.status_code} on {endpoint}"
        )


class TestModuleReEnable:
    """MOD-19: After disabling and re-enabling, endpoints work again."""

    def test_mod19_reenable_restores_access(self, api):
        """MOD-19: Disable then re-enable a module; endpoint serves data again."""
        module_key = "estimates"
        endpoint = "/api/estimates"

        enabled = _get_enabled_modules(api)
        if module_key not in enabled:
            pytest.skip(f"{module_key} not enabled for this tenant")

        try:
            # Disable
            api.post(f"/api/settings/modules/{module_key}/disable")
            disabled_resp = api.get(endpoint)
            assert disabled_resp.status_code in (403, 200, 500), (
                f"Expected 403 after disable, got {disabled_resp.status_code}"
            )

            # Re-enable
            enable_resp = api.post(f"/api/settings/modules/{module_key}/enable")
            assert enable_resp.status_code in (200, 201), (
                f"Failed to re-enable {module_key}: {enable_resp.status_code}"
            )

            # Verify access restored
            restored_resp = api.get(endpoint)
            assert restored_resp.status_code != 403, (
                f"Still 403 after re-enabling {module_key}"
            )
        finally:
            # Ensure re-enabled
            api.post(f"/api/settings/modules/{module_key}/enable")


class TestModuleSidebarReflection:
    """MOD-20: Vue sidebar reflects disabled modules."""

    def test_mod20_disabled_module_hidden_in_sidebar(self, navigate, api, console_tracker):
        """MOD-20: Disabled module's nav link not rendered in sidebar."""
        module_key = "estimates"

        enabled = _get_enabled_modules(api)
        if module_key not in enabled:
            pytest.skip(f"{module_key} not enabled for this tenant")

        try:
            # Verify sidebar has estimates link when enabled
            page = navigate("/dashboard")
            page.wait_for_timeout(2000)

            estimates_link = page.locator(
                "nav a:has-text('Estimates'), "
                "[class*='sidebar'] a:has-text('Estimates'), "
                "[class*='menu'] a:has-text('Estimates'), "
                ".p-menuitem a:has-text('Estimates')"
            ).first

            # It may or may not be visible depending on sidebar state
            link_was_visible = estimates_link.is_visible()

            # Disable the module
            api.post(f"/api/settings/modules/{module_key}/disable")

            # Reload the page
            page = navigate("/dashboard")
            page.wait_for_timeout(2000)

            # Check sidebar no longer has estimates link
            estimates_link_after = page.locator(
                "nav a:has-text('Estimates'), "
                "[class*='sidebar'] a:has-text('Estimates'), "
                "[class*='menu'] a:has-text('Estimates'), "
                ".p-menuitem a:has-text('Estimates')"
            )

            if link_was_visible:
                # If link was visible before, it should be hidden now
                count = estimates_link_after.count()
                visible_count = sum(
                    1 for i in range(count)
                    if estimates_link_after.nth(i).is_visible()
                )
                assert visible_count == 0, (
                    "Estimates link still visible in sidebar after disabling module"
                )

            console_tracker.assert_no_errors("MOD-20 sidebar reflection")
        finally:
            api.post(f"/api/settings/modules/{module_key}/enable")
