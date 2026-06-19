"""
gdx_dispatch/core/tenant_ctx.py — request-scoped tenant context for module-level state.

Some older routers (pricing.py, communications.py) keep module-level dicts
keyed nominally by "setting name" but actually shared across all tenants
in the same worker process. Passing `request: Request` through every
helper function to fix this would require ~50 signature changes per file.

Instead, this module exposes a ContextVar that's populated by a FastAPI
dependency. Helpers read the ContextVar at the top and route their
reads/writes through a per-tenant slot dict.

Usage from a router handler:

    from gdx_dispatch.core.tenant_ctx import bind_tenant_context

    @router.get("/api/pricing/settings")
    def get_settings(
        request: Request,
        _tenant: str = Depends(bind_tenant_context),
        _user: dict = Depends(get_current_user),
    ):
        settings = _per_tenant_settings()  # reads ContextVar
        ...

Usage from a helper:

    from gdx_dispatch.core.tenant_ctx import current_tenant_id

    def _get_margin(customer_type: str) -> float:
        tid = current_tenant_id()
        settings = _PRICING_SETTINGS_BY_TENANT.setdefault(tid, deepcopy(DEFAULTS))
        ...
"""
from __future__ import annotations

import contextvars

from fastapi import Request

# Default "_default" is used when no tenant context is bound (tests, cron jobs,
# helpers called outside a request). All data written under _default is still
# isolated from real tenant data because real requests always bind a real
# tenant_id before touching module state.
_current_tenant: contextvars.ContextVar[str] = contextvars.ContextVar(
    "gdx_current_tenant_id", default="_default"
)


def current_tenant_id() -> str:
    """Return the tenant_id bound to the current request, or '_default'."""
    return _current_tenant.get()


def set_tenant_id(tenant_id: str) -> contextvars.Token:
    """Bind a tenant_id to the current context. Returns token for reset()."""
    return _current_tenant.set(tenant_id or "_default")


def bind_tenant_context(request: Request) -> str:
    """FastAPI dependency: extract tenant_id from request.state and bind it.

    Safe to call on every request. Returns the bound tenant_id for
    convenience (handlers that don't need it can ignore the return value).
    If no tenant is resolved, binds '_default' — callers that need to
    enforce tenancy should validate request.state.tenant themselves and
    return 400/401 as appropriate.
    """
    tenant = getattr(request.state, "tenant", None) or {}
    tid = ""
    if isinstance(tenant, dict):
        tid = str(tenant.get("id") or "").strip()
    set_tenant_id(tid or "_default")
    return tid or "_default"
