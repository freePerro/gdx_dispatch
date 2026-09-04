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


# ── Commit 2: control-plane residue in code ────────────────────────────────


@pytest.mark.parametrize(
    "modname",
    ["gdx_dispatch.models.platform_extensions", "gdx_dispatch.core.events"],
)
def test_platform_orm_is_gone(modname: str) -> None:
    """16 marketplace-era tables (OAuthClient, BillingAccount, MeterEvent, …) and
    the EventOutbox helper that was their only importer. None of the tables
    existed on prod."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(modname)


def test_control_models_carry_no_saas_state() -> None:
    from gdx_dispatch.control import models as cm

    assert not hasattr(cm, "TenantModuleGrant")
    assert not hasattr(cm, "ServiceAccount")
    cols = {c.name for c in cm.Tenant.__table__.columns}
    assert "subscription_status" not in cols, cols
    assert "stripe_connect_account_id" not in cols, cols


def test_ambient_tenant_has_no_subscription_status() -> None:
    from gdx_dispatch.core.tenant import single_tenant

    assert "subscription_status" not in single_tenant()


def test_module_gate_has_no_control_plane_fallback() -> None:
    """``is_module_enabled`` used to fall through to the control-plane
    ``tenant_module_grants`` table (0 rows on prod). Absence assertion over the
    WHOLE module source, so reintroducing the query in any helper (not just
    the gate function) turns this red. The module's comments name the table
    only as "the control-plane fallback", never by its literal name."""
    import inspect

    from gdx_dispatch.core import modules

    assert "tenant_module_grants" not in inspect.getsource(modules)


def test_backend_ignores_client_tenant_header() -> None:
    """The ``x-tenant-id`` request header was multi-tenant residue that every
    client sent and four backend readers used as a fallback label. A client
    must no longer be able to stamp a tenant id into logs or metrics."""
    from fastapi import Request

    from gdx_dispatch.core.ai_router import _tenant_id
    from gdx_dispatch.core.ai_usage_logger import _tenant_id_from_request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/x",
        "headers": [(b"x-tenant-id", b"forged-tenant")],
        "query_string": b"",
    }
    req = Request(scope)
    req.state.tenant = {"id": "real-tenant"}
    assert _tenant_id(req) == "real-tenant"
    assert _tenant_id_from_request(req) == "real-tenant"

    # With no server-verified tenant the header must NOT fill the gap.
    req2 = Request(dict(scope))
    req2.state.tenant = {}
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        _tenant_id(req2)
    with pytest.raises(HTTPException):
        _tenant_id_from_request(req2)


# ── Commit 4: the engine-registry shim, the scanners, the ops tools ──────────


def test_engine_registry_shim_is_gone() -> None:
    """S10. ``engine_registry.get_engine(tenant_id, db_url)`` always returned the
    one app engine; its three call sites now use ``app_engine`` / ``SessionLocal``."""
    from gdx_dispatch.core import database, tenant

    for name in ("engine_registry", "EngineRegistry", "_SingleEngineRegistry", "_lookup_tenant"):
        assert not hasattr(tenant, name), name
    for name in ("_decrypt_db_url", "control_engine", "CONTROL_DATABASE_URL"):
        assert not hasattr(database, name), name


@pytest.mark.parametrize(
    "modname",
    [
        "gdx_dispatch.tools.migrate_monthly_budgets",
        "gdx_dispatch.tools.encrypt_qb_token_store_rows",
        "gdx_dispatch.tools.migrate_tenant_typed_catalogs",
        "gdx_dispatch.tools.backfill_role_permissions",
        "gdx_dispatch.tools.backfill_customer_phone_hash",
        "gdx_dispatch.tools.add_leads_perms_to_role_snapshots",
    ],
)
def test_control_plane_walking_tool_is_gone(modname: str) -> None:
    """S19. One-shot migrations that already ran and then SELECTed
    ``tenants.db_url_enc``, a column the squash dropped — unrunnable forever."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(modname)


def test_surviving_tools_resolve_the_one_database() -> None:
    """S19. The rewritten tools take DATABASE_URL, not a control-plane walk."""
    import inspect

    from gdx_dispatch.tools import (
        pave_tenant_db,
        router_sql_live_audit,
        tenant_plane_schema_drift,
        tenant_schema_drift_check,
        test_residue_sweep,
    )

    for mod in (pave_tenant_db, router_sql_live_audit, tenant_plane_schema_drift, tenant_schema_drift_check, test_residue_sweep):
        src = inspect.getsource(mod)
        assert "FROM tenants" not in src, mod.__name__
        assert "CONTROL_DATABASE_URL" not in src, mod.__name__


def test_drift_scanner_has_no_multi_tenant_rules() -> None:
    """S17. The fixture rule warned on every TestClient test lacking the legacy
    grants table (71 of 86 warnings); the platform rule waited forever for a
    file that left with the collapse."""
    from gdx_dispatch.tools import drift_scanner

    assert not hasattr(drift_scanner, "check_test_fixture_patterns")
    assert not hasattr(drift_scanner, "check_platform_schema_tests_exist")


def test_dead_scanner_baselines_are_gone() -> None:
    """S16. Every path in these baselines carried the pre-rename ``gdx/``
    prefix, so they matched nothing and suppressed nothing."""
    root = _HERE.parent
    assert not (root / ".tenant_id_shape_baseline").exists()
    assert not (root / ".cross_plane_import_baseline").exists()


def test_tenant_plane_baseline_is_current() -> None:
    """S16. The surviving baseline was re-frozen from real paths, so the scanner
    reports 0 net-new on a clean tree and CAN report 1 on a new redundant
    filter (proved by hand with a probe file on 2026-09-03)."""
    import json

    root = _HERE.parent
    entries = json.loads((root / ".tenant_plane_redundant_filter_baseline").read_text())
    assert entries, "baseline is empty"
    assert all(e.startswith("gdx_dispatch/") for e in entries), [e for e in entries if not e.startswith("gdx_dispatch/")][:3]
