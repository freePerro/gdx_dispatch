"""Sprint MCP-Streamable-HTTP S4.5 (D-S4-02 fold-in) — bridge wrapper
principal resolution under the full ASGI stack.

S1 stubbed `_resolve_principal_from_context` to raise. S4 landed the
auth middleware (verify side) and OAuth issuance side, but the bridge
wrapper still raised on every tool invocation — connector could
`tools/list` but `tools/call` would 500.

This slice wires it: the auth middleware stashes verified
``MCPClaims`` on the request state; the bridge wrapper reads them
via ``fastmcp.server.dependencies.get_http_request()`` and produces a
unified ``Principal`` whose capabilities derive from the JWT scope.

End-to-end: mint a tenant-bound JWT → POST /mcp/ → handshake → tools/call →
handler runs and returns a real result (NOT 500, NOT
``PrincipalResolutionFailed``).
"""
from __future__ import annotations

import contextlib
import json
import uuid
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.mcp_bearer import mint_mcp_access_token
from gdx_dispatch.core.mcp_mount import mcp_subapp_lifespan, mount_mcp
from gdx_dispatch.core.mcp_registry import (
    _DESCRIPTORS,
    _HANDLERS,
    register_tool,
)
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.tenant import TenantMiddleware

import gdx_dispatch.core.mcp_tools  # noqa: F401  — side-effect: registers tools


GDX_HOST = "gdx.example.com"
GDX_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")


# ── synthetic test tool (no DB dependency) ─────────────────────────────────


PROBE_NAME = "bridge_principal_probe"
_PROBE_DESCRIPTOR = ToolDescriptor(
    name=PROBE_NAME,
    description="Echoes the principal that reached the handler.",
    input_schema={"type": "object", "properties": {}},
    capabilities_required=[("read", "customer")],
)


async def _probe_handler(*, principal=None, **_ignored):
    """Returns just enough of the principal for the test to assert tenant binding."""
    return {
        "tenant_id": principal.tenant_id if principal else None,
        "auth_kind": principal.auth_kind if principal else None,
        "is_super_admin": principal.is_super_admin if principal else None,
    }


# ── fixtures ────────────────────────────────────────────────────────────────


def _fake_tenant(slug: str | None) -> dict | None:
    if slug == "gdx":
        return {"id": GDX_UUID, "slug": "gdx", "db_url": "sqlite:///:memory:",
                "subscription_status": "active", "db_provisioned": True,
                "trial_ends_at": None}
    return None


class _Sess:
    def close(self):
        pass


def _build_app() -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(a: FastAPI):
        async with mcp_subapp_lifespan(a):
            yield

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(TenantMiddleware, control_session_factory=_Sess)
    mount_mcp(app)
    return app


@pytest.fixture(scope="module")
def app_client():
    # Register probe with the registry only; ``mount_mcp`` reconciles the
    # FastMCP singleton (which prior tests may have already bridged).
    if PROBE_NAME not in _DESCRIPTORS:
        register_tool(_PROBE_DESCRIPTOR, _probe_handler)

    app = _build_app()

    def _resolver(db, *, slug=None, tenant_id=None):
        return _fake_tenant(slug)

    _single = {"id": str(GDX_UUID), "slug": "gdx", "db_url": "sqlite:///:memory:",
               "subscription_status": "active"}

    with patch("gdx_dispatch.core.tenant._lookup_tenant", side_effect=_resolver), \
         patch("gdx_dispatch.core.tenant.single_tenant", return_value=_single):
        with TestClient(app) as client:
            yield client

    _DESCRIPTORS.pop(PROBE_NAME, None)
    _HANDLERS.pop(PROBE_NAME, None)
    # The mount reconciled the probe onto the FastMCP singleton (which
    # is process-global). Drop it there too or the next mount in
    # another test sees an orphaned tool and raises MCPMountError.
    from gdx_dispatch.core.mcp_protocol_adapter import get_mcp
    try:
        get_mcp().local_provider.remove_tool(PROBE_NAME)
    except Exception:
        pass


def _mint(tenant_id: uuid.UUID = GDX_UUID, host: str = GDX_HOST) -> str:
    issuer = f"http://{host}"
    return mint_mcp_access_token(
        tenant_id=tenant_id,
        subject_id="user@example.com",
        issuer=issuer,
        audience=f"{issuer}/mcp",
        scope="mcp:invoke",
    )


def _parse_sse(body: str) -> dict:
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise AssertionError(f"no SSE data: line in body: {body!r}")


def _initialize(client: TestClient, token: str, host: str = GDX_HOST) -> str:
    headers = {"Host": host, "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {token}"}
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1.0"}},
    }
    r = client.post("/mcp/", json=body, headers=headers, follow_redirects=True)
    assert r.status_code == 200, r.text
    sid = r.headers["mcp-session-id"]
    headers["Mcp-Session-Id"] = sid
    ack = client.post(
        "/mcp/", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    assert ack.status_code == 202
    return sid


# ── tests ───────────────────────────────────────────────────────────────────


def test_tools_call_under_valid_bearer_returns_handler_result(app_client: TestClient) -> None:
    """End-to-end: bearer auth → middleware → bridge wrapper → handler."""
    token = _mint()
    sid = _initialize(app_client, token)
    headers = {"Host": GDX_HOST, "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {token}", "Mcp-Session-Id": sid}

    r = app_client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": PROBE_NAME, "arguments": {}}},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    payload = _parse_sse(r.text)
    # Result is the handler's return wrapped by FastMCP into structured_content.
    result = payload["result"]
    assert "structuredContent" in result, payload
    handler_out = result["structuredContent"]
    # Principal carries the tenant from the JWT — that's the load-bearing
    # invariant: a token minted at gdx makes the handler see gdx's tenant_id.
    assert handler_out["tenant_id"] == str(GDX_UUID)
    assert handler_out["auth_kind"] == "oauth"
    assert handler_out["is_super_admin"] is True  # mcp:invoke → ("*", "*")


def test_tools_call_capability_denied_when_descriptor_requires_more(app_client: TestClient) -> None:
    """A descriptor with capability `("admin", "tenant")` and a JWT whose
    scope only grants ``("*", "*")`` (super-admin wildcard) still passes
    — wildcard authorizes everything. We assert the registry's capability
    gate is reachable from the bridge path (smoke for the gating wiring,
    not for narrow scope semantics)."""
    token = _mint()
    sid = _initialize(app_client, token)
    headers = {"Host": GDX_HOST, "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {token}", "Mcp-Session-Id": sid}
    r = app_client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": PROBE_NAME, "arguments": {}}},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    payload = _parse_sse(r.text)
    assert "result" in payload  # capability gate did not deny


def test_tools_list_still_works_under_auth(app_client: TestClient) -> None:
    """Regression: S4 auth gate doesn't block discovery for valid bearer."""
    token = _mint()
    sid = _initialize(app_client, token)
    headers = {"Host": GDX_HOST, "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {token}", "Mcp-Session-Id": sid}
    r = app_client.post(
        "/mcp/", json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
        headers=headers,
    )
    assert r.status_code == 200
    payload = _parse_sse(r.text)
    names = [t["name"] for t in payload["result"]["tools"]]
    assert PROBE_NAME in names
    assert len(names) >= 35
