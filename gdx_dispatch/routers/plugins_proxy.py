"""Core-app proxy for /api/plugins/* → the plugin-host container.

The core app authenticates the user, resolves the tenant's enabled modules, and
forwards the request to plugin-host with the principal as X-GDX-* headers.
plugin-host trusts those because it's internal-only and the core app is its sole
caller.

Security: any client-supplied X-GDX-* header is STRIPPED before forwarding and
replaced with server-authoritative values — otherwise a user could spoof
X-GDX-Modules to reach a plugin they aren't granted. See ADR-013.
"""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import enabled_module_keys
from gdx_dispatch.plugin_api.context import H_MODULES, H_ROLE, H_TENANT, H_USER
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(prefix="/api/plugins", tags=["plugins"])

# Headers that must not be forwarded verbatim (hop-by-hop + recomputed downstream).
_DROP = {"host", "content-length", "connection", "transfer-encoding", "te", "upgrade"}


def _plugin_host_url() -> str:
    return os.getenv("PLUGIN_HOST_URL", "http://plugin-host:8000").rstrip("/")


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_to_plugin_host(
    path: str,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    tenant = getattr(request.state, "tenant", {}) or {}
    tenant_id = str(tenant.get("id", "") or "")
    modules = enabled_module_keys(db, tenant_id) if tenant_id else set()

    # Forward incoming headers EXCEPT hop-by-hop and any client-supplied X-GDX-*
    # (those are ours to set — never trust the client's copy).
    fwd = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _DROP and not k.lower().startswith("x-gdx-")
    }
    fwd[H_TENANT] = tenant_id
    fwd[H_USER] = str(user.get("sub") or user.get("user_id") or "")
    fwd[H_ROLE] = str(user.get("role") or "")
    fwd[H_MODULES] = ",".join(sorted(modules))

    body = await request.body()
    url = f"{_plugin_host_url()}/api/plugins/{path}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        upstream = await client.request(
            request.method, url, params=request.query_params, content=body, headers=fwd
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )
