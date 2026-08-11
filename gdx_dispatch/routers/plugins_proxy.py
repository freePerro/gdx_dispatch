"""Core-app proxy for /api/plugins/* → the plugin-host container.

The core app authenticates the user, resolves the tenant's enabled modules, and
forwards the request to plugin-host with the principal as X-GDX-* headers.
plugin-host trusts those because it's internal-only and the core app is its sole
caller.

Security: any client-supplied X-GDX-* header is STRIPPED before forwarding and
replaced with server-authoritative values — otherwise a user could spoof
X-GDX-Modules to reach a plugin they aren't granted. See ADR-013.

Two further rules live here, both learned the hard way (2026-08-11 audit):

1. The forwarded path is validated BEFORE the upstream URL is built. httpx
   resolves dot segments per RFC 3986, so a path of
   ``chipricing/../../../internal/browser/credentials`` used to leave here as
   ``http://plugin-host:8000/internal/browser/credentials`` — plugin-host's
   internal, owner-and-consent-gated saved-login store, reached by any
   authenticated user. The plugin key and the upstream URL must be derived from
   the SAME already-validated path, or the gate below guards a different
   request than the one that gets sent.

2. Authorization is per plugin (core/plugin_permissions.py) and fails CLOSED:
   a path whose plugin key can't be determined is refused, never forwarded.
"""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import _load_user_permissions, enabled_module_keys
from gdx_dispatch.core.plugin_permissions import (
    PLUGIN_KEY_RE,
    action_for_method,
    may_use_plugin,
)
from gdx_dispatch.plugin_api.context import H_MODULES, H_ROLE, H_TENANT, H_USER
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(prefix="/api/plugins", tags=["plugins"])

# Headers that must not be forwarded verbatim (hop-by-hop + recomputed downstream).
_DROP = {"host", "content-length", "connection", "transfer-encoding", "te", "upgrade"}


def _plugin_host_url() -> str:
    return os.getenv("PLUGIN_HOST_URL", "http://plugin-host:8000").rstrip("/")


_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]

# Path segments that must never survive to the upstream URL. "" catches the
# doubled slash in `/api/plugins//x` (which would otherwise derive an empty
# plugin key); "." and ".." are the traversal.
_BAD_SEGMENTS = {"", ".", ".."}


def _clean_subpath(path: str) -> str:
    """Validated sub-path under /api/plugins, or raise 400.

    Rejects rather than sanitizes: silently rewriting a traversal would send a
    request the caller didn't ask for, and there is no legitimate plugin route
    containing an empty or dot segment. uvicorn percent-decodes into the ASGI
    path, so `%2e%2e` arrives here already as `..` and is caught by the same
    check — the reason this validates the DECODED value.
    """
    stripped = (path or "").strip("/")
    if not stripped:
        return ""
    segments = stripped.split("/")
    if any(seg in _BAD_SEGMENTS for seg in segments):
        raise HTTPException(status_code=400, detail="invalid plugin path")
    return "/".join(segments)


# Two routes → one handler: the bare /api/plugins (catalog list) and every
# sub-path /api/plugins/<...>. Without the bare route, GET /api/plugins (no
# trailing slash) wouldn't match {path:path} and core would 404 it.
@router.api_route("", methods=_METHODS)
@router.api_route("/{path:path}", methods=_METHODS)
async def proxy_to_plugin_host(
    request: Request,
    path: str = "",
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    tenant = getattr(request.state, "tenant", {}) or {}
    tenant_id = str(tenant.get("id", "") or "")
    modules = enabled_module_keys(db, tenant_id) if tenant_id else set()

    # Validate FIRST, then derive both the plugin key and the upstream URL from
    # the same clean value — see rule 1 in the module docstring.
    safe_path = _clean_subpath(path)

    # The bare catalog (`GET /api/plugins`) stays open to any authenticated
    # user: the nav needs to know which plugins exist, and it returns manifests,
    # not plugin data. Matched on "no sub-path at all", NOT on "empty derived
    # key" — `/api/plugins//x` has an empty first segment and must not be
    # mistaken for the catalog. _clean_subpath already refuses that one.
    if safe_path:
        plugin_key = safe_path.split("/")[0]
        # `_browser/*` is the streamed-browser surface. It has its own, stricter
        # door (owner + live manifest permission + recorded consent) in
        # routers/browser_proxy.py, which is registered ahead of this catch-all.
        # Anything reaching HERE under that prefix bypassed that door, so refuse
        # it outright rather than grading it against a plugin permission.
        if plugin_key.startswith("_"):
            raise HTTPException(status_code=404, detail="not found")
        # The key must LOOK like a plugin key, not merely fail to match a grant.
        # uvicorn decodes the path exactly once, so a double-encoded segment
        # (`%252e%252e`) arrives here as the literal text `%2e%2e` and a
        # backslash segment arrives intact — neither is a traversal from this
        # process's point of view, and both would currently be denied only
        # because no grant happens to match. That is safety by coincidence, and
        # for a wildcard holder it isn't safety at all: the segment would be
        # forwarded verbatim and left to plugin-host's router to refuse.
        if not PLUGIN_KEY_RE.match(plugin_key):
            raise HTTPException(status_code=400, detail="invalid plugin key")
        action = action_for_method(request.method)
        # Same request-scoped cache require_permission uses, so a composite
        # dependency chain resolves the set once.
        perms = getattr(request.state, "user_permissions", None)
        if perms is None:
            perms = _load_user_permissions(db, request, user)
            request.state.user_permissions = perms
        if not may_use_plugin(perms, plugin_key, action):
            raise HTTPException(
                status_code=403,
                detail=f"Missing permission: plugin.{plugin_key}.{action}",
            )

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
    # Don't append a trailing slash when the sub-path is empty (the catalog list
    # endpoint GET /api/plugins) — plugin-host serves it without one, and a
    # trailing slash 404s there. `safe_path`, not `path`: the URL must be built
    # from the value the gate above actually inspected.
    url = f"{_plugin_host_url()}/api/plugins" + (f"/{safe_path}" if safe_path else "")
    async with httpx.AsyncClient(timeout=30.0) as client:
        upstream = await client.request(
            request.method, url, params=request.query_params, content=body, headers=fwd
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )
