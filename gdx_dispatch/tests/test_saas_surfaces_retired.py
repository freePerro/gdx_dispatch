"""Absence guard for the single-tenant residue purge (round two, S12–S14 + S4/S5).

Every surface named here served a platform vendor administering *tenants* —
a superadmin console, a Jinja "tenant UI", per-tenant health scores and
metrics, control-plane module grants, a Stripe subscription reconciliation —
or was an unreachable page (every ``/legacy/*`` and ``/integrations`` handler
answered 302 with no Location header for every caller, because nothing set
the principal those handlers read).

The gate asserts absence per *handler* (module import) **and** per *route*,
because ``app.openapi()`` collapses duplicate ``(method, path)`` registrations:
removing one half of a shadowed pair changes nothing in the route table, so a
path-only check can pass while the dead handler survives.

Counterfactual: restoring any deleted module or template turns its assertion
red; re-mounting any route turns the route assertion red.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest

_HERE = pathlib.Path(__file__).resolve().parent.parent  # gdx_dispatch/

_RETIRED_MODULES = [
    "gdx_dispatch.core.tenant_ui",
    "gdx_dispatch.core.health_score",
    "gdx_dispatch.core.tenant_metrics",
    "gdx_dispatch.core.admin_modules",
    "gdx_dispatch.core.admin_ops",
    "gdx_dispatch.core.ai_recommendations",
    "gdx_dispatch.core.reconciliation",
    "gdx_dispatch.core.reconciliation_tasks",
    "gdx_dispatch.core.live_dispatch",
    "gdx_dispatch.modules.ai_health_score",
]

_RETIRED_TEMPLATES = [
    "superadmin.html",
    "tenant_base.html",
    "tenant_dashboard.html",
    "tenant_settings.html",
    "tenant_team.html",
    "integrations.html",
    "ai_quote.html",
    "audit_log.html",
    "billing.html",
    "notifications.html",
    "payment_methods.html",
    "service_areas.html",
    "usage.html",
    "webhooks.html",
    "whitelabel_settings.html",
]

# (method, path) pairs that must not be registered anywhere.
_RETIRED_ROUTES = [
    ("GET", "/superadmin"),
    ("GET", "/integrations"),
    ("GET", "/legacy/dashboard"),
    ("GET", "/legacy/settings"),
    ("POST", "/legacy/settings"),
    ("GET", "/legacy/team"),
    ("POST", "/legacy/team/invite"),
    ("GET", "/legacy/billing"),
    ("GET", "/api/admin/health-scores/"),
    ("GET", "/api/admin/health-scores/{tenant_id}"),
    ("POST", "/api/admin/health-scores/run-job"),
    ("GET", "/api/admin/metrics/summary"),
    ("GET", "/api/admin/metrics/{tenant_id}"),
    ("GET", "/api/admin/tenants/{tenant_id}/modules"),
    ("POST", "/api/admin/tenants/{tenant_id}/modules"),
    ("DELETE", "/api/admin/tenants/{tenant_id}/modules/{module_key}"),
    ("GET", "/api/admin/reconciliation"),
    ("POST", "/api/recommendations/{rec_type}/dismiss"),
]


@pytest.mark.parametrize("modname", _RETIRED_MODULES)
def test_retired_module_is_gone(modname: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(modname)


@pytest.mark.parametrize("template", _RETIRED_TEMPLATES)
def test_retired_template_is_gone(template: str) -> None:
    assert not (_HERE / "templates" / template).exists(), template


def _live_routes() -> set[tuple[str, str]]:
    """Every (METHOD, path) the app actually serves, including include_in_schema=False.

    Uses ``conftest.iter_app_routes`` because FastAPI >= 0.137 keeps included
    routers as lazy ``_IncludedRouter`` wrappers that a naive ``app.routes``
    walk cannot see (the first draft of this guard saw 12 of 1354 routes and
    could not fail for anything but the top-level ``/superadmin`` handler).
    """
    from gdx_dispatch.main import app
    from gdx_dispatch.tests.conftest import iter_app_routes

    seen: set[tuple[str, str]] = set()
    for path, route in iter_app_routes(app):
        for m in getattr(route, "methods", None) or ():
            seen.add((m.upper(), path))
    return seen


def test_walker_sees_included_routers() -> None:
    """Self-check: the walker must see a route that only an included router
    provides, or every absence assertion below is theater."""
    live = _live_routes()
    assert ("GET", "/api/customers") in live, sorted(live)[:20]
    assert len(live) > 1000, len(live)


def test_no_tenant_id_path_parameter_anywhere() -> None:
    live = _live_routes()
    offenders = sorted(p for _, p in live if "{tenant_id}" in p)
    assert offenders == [], offenders


@pytest.mark.parametrize("method,path", _RETIRED_ROUTES)
def test_retired_route_is_not_served(method: str, path: str) -> None:
    assert (method, path) not in _live_routes(), f"{method} {path} is still mounted"


def test_celery_no_longer_includes_reconciliation() -> None:
    from gdx_dispatch.core.celery_app import celery_app

    includes = list(celery_app.conf.include or [])
    assert not any("reconciliation" in name for name in includes), includes
    assert not any("reconciliation" in name for name in celery_app.tasks), sorted(
        t for t in celery_app.tasks if "reconciliation" in t
    )
