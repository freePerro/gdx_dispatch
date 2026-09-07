"""The dead-duplicates removal of 2026-09-06 stays removed.

Every assertion here is an ABSENCE or a uniqueness: a module that no longer
imports, a path the app no longer registers, a (method, path) that now has
exactly one handler — the one FastAPI always dispatched to. Presence of code
proves nothing about reachability (that is how the losing halves of these
pairs survived for months); absence of the loser and a single winner does.

Issues: #458 #459 #480 #568 #569 #571 #572 #574 #595 #599 (the dead-duplicates
removal of 2026-09-06).
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
from collections import Counter

import pytest

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-at-least-32-bytes-long-for-hs256-sha256-safety")

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def registrations() -> Counter:
    """(method, path) -> number of handlers registered, walking include_router
    wrappers the way FastAPI dispatches (conftest.iter_app_routes)."""
    from gdx_dispatch.app import app
    from gdx_dispatch.tests.conftest import iter_app_routes

    counts: Counter = Counter()
    for full_path, route in iter_app_routes(app):
        for method in getattr(route, "methods", None) or ():
            counts[(method, full_path)] += 1
    return counts


@pytest.fixture(scope="module")
def winners() -> dict[tuple[str, str], str]:
    """(method, path) -> module of the FIRST registration, i.e. the handler
    FastAPI serves."""
    from gdx_dispatch.app import app
    from gdx_dispatch.tests.conftest import iter_app_routes

    first: dict[tuple[str, str], str] = {}
    for full_path, route in iter_app_routes(app):
        endpoint = getattr(route, "endpoint", None)
        for method in getattr(route, "methods", None) or ():
            first.setdefault((method, full_path), getattr(endpoint, "__module__", ""))
    return first


# ── Modules that are gone ────────────────────────────────────────────────────

@pytest.mark.parametrize("dotted", [
    "gdx_dispatch.routers.booking",       # #458
    "gdx_dispatch.routers.po_workflow",   # #568
    "gdx_dispatch.tasks.email_poller",    # #599
])
def test_module_no_longer_exists(dotted: str) -> None:
    assert importlib.util.find_spec(dotted) is None, f"{dotted} is back"


def test_celery_no_longer_registers_the_poller() -> None:
    source = (ROOT / "core" / "celery_app.py").read_text()
    assert "email_poller" not in source


def test_models_no_longer_declare_the_dropped_tables() -> None:
    from gdx_dispatch.models.tenant_models import Base

    gone = {"po_requests", "po_request_lines", "booking_requests_router", "booking_jobs_router", "portal_booking_requests"}
    assert not (gone & set(Base.metadata.tables))


# ── Paths that are gone ──────────────────────────────────────────────────────

REMOVED_PATHS = [
    # routers/booking.py (#458) and the portal-side write it paired with
    ("GET", "/api/booking/available-slots"),
    ("POST", "/api/booking/request"),
    ("GET", "/api/booking/requests"),
    ("POST", "/api/booking/requests/{request_id}/approve"),
    ("POST", "/api/booking/requests/{request_id}/decline"),
    # ui_compat stubs (#459)
    ("GET", "/api/customers/{customer_id}/communications"),
    ("POST", "/api/customers/{customer_id}/communications"),
    # routers/mobile.py orphans found by re-running #480's shape (2026-09-07 follow-up)
    ("POST", "/api/mobile/jobs/{job_id}/status"),
    ("POST", "/api/mobile/job/{job_id}/status"),
    ("POST", "/api/mobile/clock-in"),
    ("POST", "/api/mobile/clock-out"),
    ("POST", "/api/mobile/job/{job_id}/clock-in"),
    ("POST", "/api/mobile/job/{job_id}/clock-out"),
    ("POST", "/api/mobile/job/{job_id}/notes"),
    # modules/campaigns/router.py, the module's last route (2026-09-07 follow-up)
    ("GET", "/api/campaigns/{campaign_id}/stats"),
    # routers/mobile.py orphans (#480)
    ("GET", "/api/mobile/clock-status"),
    ("GET", "/api/mobile/schedule"),
    ("GET", "/api/mobile/my-jobs"),
    ("GET", "/api/mobile/my-jobs/{job_id}"),
    ("GET", "/api/mobile/jobs/{job_id}/checklist"),
    ("POST", "/api/mobile/jobs/{job_id}/start"),
    ("GET", "/api/mobile/timecard"),
    ("POST", "/api/mobile/jobs/{job_id}/signature"),
    ("POST", "/api/mobile/job/{job_id}/signature"),
    ("POST", "/api/mobile/sync"),
    ("POST", "/api/mobile/jobs/{job_id}/transition/{status}"),
]


@pytest.mark.parametrize("method,path", REMOVED_PATHS)
def test_removed_path_is_not_registered(registrations: Counter, method: str, path: str) -> None:
    assert registrations[(method, path)] == 0, f"{method} {path} is registered again"


def test_no_portal_booking_write_remains(registrations: Counter) -> None:
    hits = [k for k in registrations if k[1].endswith("/booking")]
    assert not hits, hits


# ── Pairs that now have exactly one handler, and it is the one that served ──

RESOLVED_PAIRS = {
    ("GET", "/api/admin/permissions"): "gdx_dispatch.routers.admin_ops",          # #571
    ("GET", "/api/jobs/{job_id}/activity"): "gdx_dispatch.routers.jobs",          # #571
    ("POST", "/api/mobile/location"): "gdx_dispatch.routers.tech_locations",      # #572
    ("GET", "/api/ai/usage"): "gdx_dispatch.core.ai_usage_logger",                # #574
    ("GET", "/api/purchase-orders"): "gdx_dispatch.routers.purchase_orders",      # #568
    ("POST", "/api/purchase-orders"): "gdx_dispatch.routers.purchase_orders",
    ("PATCH", "/api/purchase-orders/{po_id}"): "gdx_dispatch.routers.purchase_orders",
    ("POST", "/api/purchase-orders/{po_id}/receive"): "gdx_dispatch.routers.purchase_orders",
    ("GET", "/api/campaigns"): "gdx_dispatch.routers.campaigns",                  # #569
    ("POST", "/api/campaigns"): "gdx_dispatch.routers.campaigns",
    ("POST", "/api/campaigns/{campaign_id}/send"): "gdx_dispatch.routers.campaigns",
    ("GET", "/api/dispatch/locations"): "gdx_dispatch.routers.tech_locations",
    ("GET", "/api/fleet/vehicles"): "gdx_dispatch.routers.fleet",
    ("POST", "/api/fleet/vehicles"): "gdx_dispatch.routers.fleet",
    ("GET", "/api/inventory/parts"): "gdx_dispatch.routers.inventory",
    ("POST", "/api/inventory/parts"): "gdx_dispatch.routers.inventory",
    ("GET", "/api/inventory/low-stock"): "gdx_dispatch.routers.inventory",
    ("GET", "/api/timeclock/status"): "gdx_dispatch.routers.timeclock",
    ("POST", "/api/timeclock/clock-in"): "gdx_dispatch.routers.timeclock",
}


@pytest.mark.parametrize("pair,module", sorted(RESOLVED_PAIRS.items()))
def test_resolved_pair_has_one_handler_and_it_is_the_canonical_one(
    registrations: Counter, winners: dict, pair: tuple[str, str], module: str
) -> None:
    assert registrations[pair] == 1, f"{pair} has {registrations[pair]} registrations"
    assert winners[pair] == module, f"{pair} is served by {winners[pair]}, expected {module}"


# ── Duplicate definitions (#595) ─────────────────────────────────────────────

@pytest.mark.parametrize("relpath,name", [
    ("routers/ai.py", "get_db_for_ai"),
    ("modules/outlook/admin_settings_router.py", "get_db_for_admin"),
])
def test_function_is_defined_exactly_once(relpath: str, name: str) -> None:
    source = (ROOT / relpath).read_text()
    assert source.count(f"def {name}(") == 1


def test_ai_router_imports_get_db_once() -> None:
    source = (ROOT / "routers" / "ai.py").read_text()
    assert "import get_db, get_db" not in source
