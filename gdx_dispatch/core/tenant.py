from __future__ import annotations

import os
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


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


class TenantMiddleware(BaseHTTPMiddleware):
    """Pin every request to the single GDX tenant.

    Phase A removed the multi-tenant resolver (subdomain / header lookup,
    trial-expiry, unknown-tenant 404). Phase C removed the per-request
    ``_current_tenant_id`` ContextVar wiring that fed the PostgreSQL GUC
    machinery. The 2026-09-03 purge removed the ``_lookup_tenant`` test stub
    (this middleware never called it) and the ``subscription_status`` key.

    ``control_session_factory`` is accepted and silently ignored — many
    call-sites and tests still pass it.
    """

    _BYPASS_PATHS = {"/health", "/docs", "/openapi.json", "/redoc", "/sw.js", "/manifest.json"}

    _BYPASS_PREFIXES = (
        "/admin/", "/pwa/", "/api/push/", "/assets",
        "/onboarding", "/stripe/webhook", "/supplier/join/",
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
