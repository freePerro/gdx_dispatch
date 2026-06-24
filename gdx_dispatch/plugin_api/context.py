"""Request context for plugin routes — the identity the core proxy forwards.

In Model B (ADR-013) plugins run in the plugin-host container, NOT the core app.
The core app authenticates the user, then proxies /api/plugins/* to plugin-host
with the principal forwarded as headers (set in step 3). plugin-host trusts these
because it is internal-network only and never exposed.

A plugin route therefore gets its identity from these forwarded headers, not from
the core TenantMiddleware (which doesn't run here). `require_module` checks the
forwarded enabled-modules set — the grant decision is made in core and carried
along, so plugin-host needs no DB round-trip just to gate.

Importing this pulls in FastAPI/SQLAlchemy — submodule, not re-exported from
plugin_api/__init__ (kept stdlib-only for host-side discovery tests).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import SessionLocal

# Header names the core proxy sets. Centralized so proxy + context can't drift.
H_TENANT = "X-GDX-Tenant-Id"
H_USER = "X-GDX-User-Id"
H_ROLE = "X-GDX-Role"
H_MODULES = "X-GDX-Modules"  # comma-separated enabled module keys


@dataclass(frozen=True)
class PluginContext:
    """Who is calling, forwarded from the core app."""

    tenant_id: str
    user_id: str
    role: str
    enabled_modules: frozenset[str]


def get_plugin_context(request: Request) -> PluginContext:
    """Build a PluginContext from the forwarded headers.

    400s if the tenant header is missing — that means the request did not come
    through the core proxy, which is the only supported caller.
    """
    h = request.headers
    tenant_id = (h.get(H_TENANT) or "").strip()
    if not tenant_id:
        raise HTTPException(
            status_code=400,
            detail="Missing tenant context — plugin routes are reachable only via the core proxy",
        )
    modules = frozenset(m.strip() for m in (h.get(H_MODULES) or "").split(",") if m.strip())
    return PluginContext(
        tenant_id=tenant_id,
        user_id=(h.get(H_USER) or "").strip(),
        role=(h.get(H_ROLE) or "").strip(),
        enabled_modules=modules,
    )


def get_plugin_db() -> Generator[Session, None, None]:
    """A DB session on the shared database. Plugins use this instead of importing
    core.database, so the import surface they bind to stays inside plugin_api."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_module(module_key: str) -> Callable:
    """Dependency: 403 unless `module_key` is in the forwarded enabled-modules set.

    Returns the PluginContext on success, so a route can both gate and read
    identity in one dependency::

        @router.get("/items")
        def list_items(ctx: PluginContext = Depends(require_module("example")),
                       db: Session = Depends(get_plugin_db)):
            ...
    """
    key = module_key.strip().lower()

    def _dep(ctx: PluginContext = Depends(get_plugin_context)) -> PluginContext:
        if key not in ctx.enabled_modules:
            raise HTTPException(status_code=403, detail=f"Module '{key}' is not enabled")
        return ctx

    return _dep
