"""Pins the 2026-08-03 multi-tenant control-plane removal.

GDX is single-tenant by decision. The SaaS control plane (platform
analytics, distributor/wholesaler dashboards, developer portal UI, status
page, task/SLA monitors, the bare /v1 public API, contractors) was removed;
these tests keep it from creeping back and — more importantly — pin the
surfaces that had to SURVIVE the removal: the /api/v1 endpoints that
garagedoorxperts.com calls, and the /api/developer/keys CRUD that manages
the API keys those calls authenticate with.
"""
from __future__ import annotations

import pytest

from gdx_dispatch.tests.conftest import app_route_paths

# Prefixes owned by removed modules. A path either equal to the prefix or
# nested under it means the surface came back (likely via a re-added mount).
_REMOVED_PREFIXES = (
    "/api/platform",       # core/platform_analytics.py
    "/api/wholesaler",     # core/wholesaler_dashboard.py
    "/api/distributor/dashboard",  # core/distributor_dashboard.py (order portal keeps /api/distributor/*)
    "/api/api-keys",       # core/developer_portal.py (duplicate of /api/developer/keys)
    "/developer",          # developer portal UI page
    "/v1",                 # core/public_api.py — the dead twin of /api/v1
    "/api/status",         # core/status_page.py
    "/status",             # status page UI
    "/api/admin/tasks",    # core/task_monitor.py
    "/admin/tasks",        # task monitor UI page
    "/api/admin/sla",      # core/sla_monitor.py
    "/admin/sla-report",   # SLA report UI page
    "/analytics",          # platform analytics UI page
    "/api/contractors",    # modules/contractors/
)

# Surfaces that the removal explicitly had to preserve.
_MUST_SURVIVE = (
    "/api/v1/landing-leads",   # garagedoorxperts.com lead form POST
    "/api/v1/listings",        # garagedoorxperts.com /used-doors SSR feed
    "/api/developer/keys",     # core/api_keys.py CRUD — manages the keys above
)


@pytest.fixture(scope="module")
def route_paths() -> set[str]:
    from gdx_dispatch.app import create_app

    return app_route_paths(create_app())


@pytest.mark.parametrize("prefix", _REMOVED_PREFIXES)
def test_removed_prefix_stays_gone(route_paths: set[str], prefix: str) -> None:
    hits = sorted(
        p for p in route_paths if p == prefix or p.startswith(prefix + "/")
    )
    assert not hits, (
        f"control-plane surface {prefix!r} reappeared in the route table: {hits} "
        "— it was removed 2026-08-03 (single-tenant decision)"
    )


@pytest.mark.parametrize("path", _MUST_SURVIVE)
def test_surviving_surface_still_mounted(route_paths: set[str], path: str) -> None:
    assert path in route_paths, (
        f"{path!r} is missing from the route table — this endpoint must survive "
        "the control-plane removal (live external integration / key management)"
    )
