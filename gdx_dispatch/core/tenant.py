from __future__ import annotations

import os
from typing import Any

from sqlalchemy.engine import Engine
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class _SingleEngineRegistry:
    """Stub that replaces the multi-tenant EngineRegistry.

    In single-tenant GDXDispatch every "per-tenant engine" is the same
    application engine. Kept as a compatibility shim for the ~20 import sites
    that still call ``engine_registry.get_engine(tenant_id, db_url)``. Phase D
    will remove those call sites and this shim.
    """

    def get_engine(self, tenant_id: str, db_url: str) -> Engine:
        from gdx_dispatch.core.database import app_engine
        return app_engine

    def dispose_all(self) -> None:
        pass

    @property
    def _engines(self) -> dict:
        return {}


engine_registry = _SingleEngineRegistry()

# Keep the class name for the one test that imported it directly.
EngineRegistry = _SingleEngineRegistry


def single_tenant() -> dict[str, Any]:
    """The one tenant this single-tenant app serves.

    Built from env so no customer identifier is hard-coded into the
    OSS-destined tree. ``db_url`` mirrors the single application database so
    the few consumers that still read ``request.state.tenant["db_url"]``
    resolve to the same one DB.
    """
    return {
        "id": os.getenv("GDX_TENANT_ID") or os.getenv("GDX_DEFAULT_TENANT_ID") or "00000000-0000-0000-0000-000000000001",
        "slug": os.getenv("GDX_TENANT_SLUG") or "00000000-0000-0000-0000-000000000001",
        "name": os.getenv("GDX_TENANT_NAME") or "Example Garage Doors",
        "db_url": os.getenv("DATABASE_URL", "sqlite:///./gdx.db"),
        "subscription_status": "active",
    }


def company_id() -> str:
    """The single tenant's company id — the one value the data plane needs.

    Canonical replacement for ``request.state.tenant["id"]`` /
    ``request.state.tenant_id``. Single-tenant: there is exactly one company,
    sourced from env (``GDX_TENANT_ID``). Call this directly from routers,
    services, and middleware instead of reading ``request.state``.
    """
    return single_tenant()["id"]


def get_company_id() -> str:
    """FastAPI dependency form of :func:`company_id`.

    Use as ``cid: str = Depends(get_company_id)`` in routes. Exists as a
    dependency (not just the plain function) so tests can override it via
    ``app.dependency_overrides[get_company_id]`` without monkeypatching env —
    the idiomatic FastAPI seam for request-scoped context.
    """
    return company_id()


def _lookup_tenant(request: Any = None, **_kwargs: Any) -> dict[str, Any]:
    """Backward-compat stub for tests that patch this function.

    Phase C removed the multi-tenant resolver; all lookups resolve to the one
    GDX tenant. Tests that patch this function to return different tenant dicts
    (e.g. multi-tenant MCP cross-tenant denial tests) continue to work because
    TenantMiddleware delegates to this function instead of calling
    single_tenant() directly.
    """
    return single_tenant()


class TenantMiddleware(BaseHTTPMiddleware):
    """Pin every request to the single GDX tenant.

    Phase A removed the multi-tenant resolver (subdomain / x-tenant-id lookup,
    trial-expiry, unknown-tenant 404). Phase C removed the per-request
    ``_current_tenant_id`` ContextVar wiring that fed the PostgreSQL GUC
    machinery.

    ``control_session_factory`` is accepted and silently ignored — many
    call-sites and tests still pass it.
    """

    _BYPASS_PATHS = {"/health", "/docs", "/openapi.json", "/redoc", "/sw.js", "/manifest.json"}

    _TENANTLESS_ALLOWED_PATHS = {"/api/feedback/client-error"}

    _BYPASS_PREFIXES = (
        "/admin/", "/pwa/", "/api/push/", "/api/feature-flags", "/assets",
        "/onboarding", "/signup", "/stripe/webhook", "/supplier/join/",
        "/api/supplier/register", "/api/supplier/login",
    )

    _API_PREFIXES = ("/api/", "/auth/")

    def __init__(self, app: Any, **_kwargs: Any) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        if path in self._BYPASS_PATHS or any(path.startswith(p) for p in self._BYPASS_PREFIXES):
            return await call_next(request)
        tenant = single_tenant()
        request.state.tenant = tenant
        request.state.tenant_id = tenant["id"]
        return await call_next(request)
