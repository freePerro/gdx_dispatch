"""Sprint MCP-Streamable-HTTP S2 — mount FastMCP at /mcp.

Verifies the HTTP-level Streamable-HTTP transport: a real client can
``initialize`` against ``/mcp``, get back a ``Mcp-Session-Id``, and
list every registered tool. This is the assertion that S1's blind
spot called for — schema parity at the wire layer, not just in-process.

Why TestClient and not httpx.ASGITransport: httpx's ASGI transport
does not invoke ASGI lifespan events, so FastMCP's session-manager
task group never starts and every request 500s with "Task group is
not initialized". TestClient does invoke lifespan, so the FastMCP
sub-app comes up cleanly.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from gdx_dispatch.core.mcp_bearer import mint_mcp_access_token


# Sprint plan: every tool in the legacy registry must reach claude.ai
# through the FastMCP transport. The __init__.py side-effect import
# registers 35 modules at app startup; this is the floor.
EXPECTED_MIN_TOOLS = 35

# Tenant host used for all S2 requests. TenantMiddleware must resolve
# this to a known tenant; we patch _lookup_tenant for the test fixture.
TENANT_HOST = "gdx.example.com"
TENANT_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_ISSUER = f"http://{TENANT_HOST}"
TENANT_AUDIENCE = f"{TENANT_ISSUER}/mcp"


def _mint_token(tenant_id: uuid.UUID = TENANT_UUID, audience: str = TENANT_AUDIENCE,
                issuer: str = TENANT_ISSUER) -> str:
    return mint_mcp_access_token(
        tenant_id=tenant_id,
        subject_id="test-subject",
        issuer=issuer,
        audience=audience,
        scope="mcp:invoke",
    )


def _auth_headers(token: str | None = None) -> dict[str, str]:
    h = {"Host": TENANT_HOST, "Accept": "application/json, text/event-stream"}
    if token is None:
        token = _mint_token()
    h["Authorization"] = f"Bearer {token}"
    return h


HEADERS_BASE = _auth_headers()

_FAKE_TENANT = {
    "id": TENANT_UUID,
    "slug": "gdx",
    "db_url": "sqlite:///:memory:",
    "subscription_status": "active",
    "db_provisioned": True,
    "trial_ends_at": None,
}


def _parse_sse_event(body: str) -> dict:
    """FastMCP's Streamable-HTTP returns SSE-framed JSON: extract `data:` line."""
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    raise AssertionError(f"no SSE `data:` line in body: {body!r}")


def _build_minimal_app():
    """Minimal FastAPI app exercising just the bits S2 owns.

    The full `create_app()` stack adds ~12 middlewares (consumer audit,
    cross-tenant access, idempotency, etc.) that query tables sqlite-test
    DBs do not provision. Those middlewares are unrelated to the mount,
    so the integration test isolates: TenantMiddleware (the route in
    front of MCP) + mount_mcp (the slice under test) + lifespan
    (FastMCP session manager startup).
    """
    import contextlib

    from fastapi import FastAPI

    from gdx_dispatch.core.mcp_mount import mcp_subapp_lifespan, mount_mcp
    from gdx_dispatch.core.tenant import TenantMiddleware

    @contextlib.asynccontextmanager
    async def lifespan(a: FastAPI):
        async with mcp_subapp_lifespan(a):
            yield

    class _Sess:
        def close(self):
            pass

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(TenantMiddleware, control_session_factory=_Sess)
    mount_mcp(app)
    return app


@pytest.fixture(scope="module")
def app_client():
    # Side-effect import populates mcp_registry before mount_mcp bridges it.
    import gdx_dispatch.core.mcp_tools  # noqa: F401

    app = _build_minimal_app()
    # Stub tenant resolution so TenantMiddleware doesn't 404 the test request.
    # Real tenant binding (token aud + gdx_tid) is S4's responsibility.
    _single = {"id": str(TENANT_UUID), "slug": "gdx", "db_url": "sqlite:///:memory:",
               "subscription_status": "active"}
    with patch("gdx_dispatch.core.tenant._lookup_tenant", return_value=_FAKE_TENANT), \
         patch("gdx_dispatch.core.tenant.single_tenant", return_value=_single):
        with TestClient(app) as client:
            yield client


def _initialize(client: TestClient) -> str:
    """Run the MCP `initialize` handshake, return the issued Mcp-Session-Id."""
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "gdx-mcp-test", "version": "1.0"},
        },
    }
    r = client.post(
        "/mcp/",
        json=init_body,
        headers=HEADERS_BASE,
        follow_redirects=True,
    )
    assert r.status_code == 200, f"initialize failed: {r.status_code} {r.text}"
    sid = r.headers.get("mcp-session-id")
    assert sid, "server did not return Mcp-Session-Id header"

    payload = _parse_sse_event(r.text)
    assert payload["result"]["protocolVersion"] == "2025-06-18"
    # ack required by spec before any other request:
    headers = {**HEADERS_BASE, "Mcp-Session-Id": sid}
    ack = client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    assert ack.status_code == 202
    return sid


def test_mcp_endpoint_does_not_serve_spa_html(app_client: TestClient) -> None:
    """Pre-S2 bug: /mcp returned the Vue SPA index.html. After S2 it must not."""
    r = app_client.get("/mcp/", headers=_auth_headers(), follow_redirects=True)
    body = r.text.lower()
    assert "<!doctype html" not in body, "SPA catch-all is shadowing /mcp"
    assert "<html" not in body, "SPA catch-all is shadowing /mcp"


def test_mcp_initialize_returns_session_id_and_capabilities(app_client: TestClient) -> None:
    sid = _initialize(app_client)
    assert sid  # non-empty


def test_mcp_tools_list_returns_full_toolset(app_client: TestClient) -> None:
    sid = _initialize(app_client)
    headers = {**HEADERS_BASE, "Mcp-Session-Id": sid}
    r = app_client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    payload = _parse_sse_event(r.text)
    tools = payload["result"]["tools"]
    assert len(tools) >= EXPECTED_MIN_TOOLS, (
        f"FastMCP tools/list returned {len(tools)} tools; "
        f"expected >= {EXPECTED_MIN_TOOLS}. The mount/bridge wiring is "
        "incomplete or the registry is partially populated."
    )

    from gdx_dispatch.core.mcp_registry import list_tools as registry_list

    # Registry stores dotted names; bridge translates to underscore
    # form for claude.ai compatibility (its name validator rejects dots).
    registry_names = {d.name.replace(".", "_") for d in registry_list()}
    fastmcp_names = {t["name"] for t in tools}
    missing = registry_names - fastmcp_names
    assert not missing, f"tools missing from FastMCP transport: {sorted(missing)}"


def test_mcp_tool_input_schema_matches_descriptor(app_client: TestClient) -> None:
    """S1 blind spot: confirm the wire-level schema equals the descriptor schema."""
    sid = _initialize(app_client)
    headers = {**HEADERS_BASE, "Mcp-Session-Id": sid}
    r = app_client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        headers=headers,
    )
    payload = _parse_sse_event(r.text)
    tools_by_name = {t["name"]: t for t in payload["result"]["tools"]}

    from gdx_dispatch.core.mcp_registry import list_tools as registry_list

    for descriptor in registry_list():
        wire = tools_by_name.get(descriptor.name.replace(".", "_"))
        assert wire is not None, f"{descriptor.name} not on the wire"
        assert wire["inputSchema"] == descriptor.input_schema, (
            f"{descriptor.name}: wire schema differs from descriptor — "
            f"FastMCP serializer transformed it. Wire={wire['inputSchema']!r} "
            f"Descriptor={descriptor.input_schema!r}"
        )
