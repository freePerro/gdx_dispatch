"""Sprint MCP-Streamable-HTTP S4 — ASGI middleware enforcing bearer
auth on the MCP transport.

Sits in front of FastMCP's ``http_app()`` ASGI sub-app. For every HTTP
request it:

1. Reads ``request.state.tenant`` (set by the parent app's
   ``TenantMiddleware``) — that's the request-side tenant truth.
2. Pulls the ``Authorization: Bearer ...`` header.
3. Calls ``verify_mcp_bearer(...)`` with the expected issuer
   (``https://<host>``), expected audience (``https://<host>/mcp``),
   and expected tenant UUID. Any mismatch → 403.
4. On success, stashes the verified ``MCPClaims`` on the ASGI
   ``scope["state"]`` so the bridge wrapper can pull a Principal
   from it without re-parsing the token.

The middleware does NOT inspect the JSON-RPC body — every request to
the mounted sub-app is gated, including ``initialize``. claude.ai's
connector flow expects bearer auth from the very first request.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from gdx_dispatch.core.mcp_bearer import (
    BearerInvalid,
    BearerKeyNotConfigured,
    MCPClaims,
    verify_mcp_bearer,
)

logger = logging.getLogger(__name__)


def _scope_header(scope: dict, name: bytes) -> str | None:
    """Case-insensitive ASGI header lookup."""
    for k, v in scope.get("headers", ()):
        if k.lower() == name:
            try:
                return v.decode("latin-1")
            except UnicodeDecodeError:
                return None
    return None


def _expected_issuer(scope: dict) -> str:
    """Derive ``https://<host>`` from the inbound request scope.

    Mirrors ``well_known_manifest.request_base_url`` so the issuer the
    middleware checks matches the issuer the discovery doc advertised.
    """
    host = _scope_header(scope, b"host")
    if not host:
        raise BearerInvalid("request has no Host header")
    proto = (
        _scope_header(scope, b"x-forwarded-proto")
        or scope.get("scheme")
        or "https"
    )
    return f"{proto}://{host}"


def _expected_audience(scope: dict) -> str:
    return f"{_expected_issuer(scope)}/mcp"


def _request_tenant_id(scope: dict) -> str | None:
    """Pull the tenant UUID set by the parent app's ``TenantMiddleware``.

    Starlette's ``request.state`` is backed by ``scope["state"]`` on
    mounted sub-apps; ``TenantMiddleware`` writes ``state.tenant`` and
    ``state.tenant_id``. Returns the bare UUID (string form).
    """
    state = scope.get("state") or {}
    tenant = state.get("tenant") or {}
    if isinstance(tenant, dict) and tenant.get("id"):
        return str(tenant["id"])
    if state.get("tenant_id"):
        return str(state["tenant_id"])
    return None


async def _send_error(
    send,
    status: int,
    error: str,
    description: str,
    *,
    realm: str = "mcp",
) -> None:
    """Emit an OAuth-shaped JSON error with ``WWW-Authenticate`` per RFC 6750."""
    body = json.dumps({"error": error, "error_description": description}).encode()
    challenge = f'Bearer realm="{realm}", error="{error}", error_description="{description}"'
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", challenge.encode("latin-1")),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


class MCPBearerAuthMiddleware:
    """Pure ASGI middleware — no Starlette / FastAPI dependency.

    FastMCP's ``http_app(middleware=[...])`` accepts plain ASGI
    middleware factories, so wiring is just::

        from fastmcp.server.middleware import ASGIMiddleware
        mcp.http_app(middleware=[ASGIMiddleware(MCPBearerAuthMiddleware)])

    A pure ASGI implementation also lets tests exercise the middleware
    without spinning up the full FastMCP transport.
    """

    def __init__(self, app: Any, *, allow_unauthenticated: bool = False) -> None:
        self.app = app
        # ``allow_unauthenticated`` exists ONLY for the duration of a slice
        # boundary or local debugging — production must never set it. Caller
        # gets it via an explicit env var; default is to reject. The
        # constructor takes a kwarg so a test can opt-in without env mocking.
        self._allow_unauthenticated = allow_unauthenticated

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        try:
            expected_issuer = _expected_issuer(scope)
            expected_audience = _expected_audience(scope)
        except BearerInvalid as exc:
            await _send_error(send, 400, "invalid_request", str(exc))
            return

        tenant_id = _request_tenant_id(scope)
        if not tenant_id:
            # No tenant resolved → either the parent app's middleware order
            # is wrong or the host is unknown. Either way the only safe
            # answer is reject — the alternative is letting a token authenticate
            # against an unbound transport.
            await _send_error(
                send,
                403,
                "tenant_unresolved",
                "request has no resolved tenant; mount must run behind TenantMiddleware",
            )
            return

        auth = _scope_header(scope, b"authorization")
        if not auth or not auth.lower().startswith("bearer "):
            if self._allow_unauthenticated:
                logger.warning(
                    "mcp_bearer.unauthenticated_pass tenant=%s path=%s "
                    "(allow_unauthenticated=True; production MUST NOT use this)",
                    tenant_id,
                    scope.get("path"),
                )
                await self.app(scope, receive, send)
                return
            await _send_error(send, 401, "invalid_token", "missing bearer token")
            return
        token = auth.split(" ", 1)[1].strip()

        try:
            claims = verify_mcp_bearer(
                token,
                expected_issuer=expected_issuer,
                expected_audience=expected_audience,
                expected_tenant_id=tenant_id,
            )
        except BearerKeyNotConfigured as exc:
            # Server-side misconfiguration — return 500 not 401, since this
            # is not an auth failure.
            await _send_error(send, 500, "server_error", str(exc))
            return
        except BearerInvalid as exc:
            await _send_error(send, 403, "invalid_token", str(exc))
            return

        # Stash on scope state for the bridge wrapper.
        state = scope.setdefault("state", {})
        state["mcp_claims"] = claims

        await self.app(scope, receive, send)


__all__ = ["MCPBearerAuthMiddleware", "MCPClaims"]
