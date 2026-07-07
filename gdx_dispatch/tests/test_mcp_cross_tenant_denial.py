"""Sprint MCP-Streamable-HTTP S4 — cross-tenant denial at the transport.

The plan's verification gate: a token minted at gdx.* must return 403
against another tenant's /mcp. Any exception means the security
invariant is broken and the lab/prod gate (S6/S7) is blocked.

This test mirrors the minimal-app pattern from S2's mount test —
TenantMiddleware + the FastMCP sub-app — but exercises every failure
mode the bearer middleware enforces.
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
from gdx_dispatch.core.tenant import TenantMiddleware

import gdx_dispatch.core.mcp_tools  # noqa: F401  — side-effect: registers tools


GDX_HOST = "gdx.example.com"
ACME_HOST = "acme.example.com"
GDX_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
ACME_UUID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _fake_tenant(host: str) -> dict:
    """Map host → tenant dict in the same shape `_lookup_tenant` returns."""
    if host.startswith("gdx."):
        return {"id": GDX_UUID, "slug": "gdx", "db_url": "sqlite:///:memory:",
                "subscription_status": "active", "db_provisioned": True,
                "trial_ends_at": None}
    if host.startswith("acme."):
        return {"id": ACME_UUID, "slug": "acme", "db_url": "sqlite:///:memory:",
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


def _scheme_from_test() -> str:
    """TestClient defaults to ``http://``; bearer middleware also reads
    that scheme. Must match what the JWT was minted with."""
    return "http"


def _mint(tenant_id: uuid.UUID, host: str) -> str:
    issuer = f"{_scheme_from_test()}://{host}"
    return mint_mcp_access_token(
        tenant_id=tenant_id,
        subject_id="user@example.com",
        issuer=issuer,
        audience=f"{issuer}/mcp",
        scope="mcp:invoke",
    )


def _initialize_payload() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "gdx-mcp-test", "version": "1.0"},
        },
    }


@pytest.fixture(scope="module")
def app_client():
    app = _build_app()

    def _resolver(db, *, slug=None, tenant_id=None):
        # Slug is what TenantMiddleware extracts from the Host header
        # (the part before the first dot). Map back to a fake tenant.
        if slug:
            return _fake_tenant(f"{slug}.example.com")
        return None

    _single = {"id": str(GDX_UUID), "slug": "gdx", "db_url": "sqlite:///:memory:",
               "subscription_status": "active"}

    with patch("gdx_dispatch.core.tenant._lookup_tenant", side_effect=_resolver), \
         patch("gdx_dispatch.core.tenant.single_tenant", return_value=_single):
        with TestClient(app) as client:
            yield client


def _post_init(client: TestClient, host: str, token: str | None) -> int:
    headers = {"Host": host, "Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    r = client.post("/mcp/", json=_initialize_payload(), headers=headers,
                    follow_redirects=True)
    return r


# ── happy path: same-tenant token reaches the transport ─────────────────────


def test_same_tenant_token_passes_through_auth(app_client: TestClient) -> None:
    token = _mint(GDX_UUID, GDX_HOST)
    r = _post_init(app_client, GDX_HOST, token)
    assert r.status_code == 200, r.text
    sid = r.headers.get("mcp-session-id")
    assert sid, "MCP transport never reached its handler — auth blocked unexpectedly"


# ── cross-tenant denial — the load-bearing security gate ────────────────────


def test_gdx_token_at_acme_mcp_returns_401(app_client: TestClient) -> None:
    """The plan's verification gate. Token minted at gdx → denied at acme.

    401 (not 403) per RFC 6750: the token is `invalid_token` FOR THIS
    resource (aud/gdx_tid mismatch), and 401 tells the client to re-run
    authorization. 2026-07-07 audit: claude.ai's connector treated the
    old 403 as terminal and never re-authenticated."""
    token = _mint(GDX_UUID, GDX_HOST)
    r = _post_init(app_client, ACME_HOST, token)
    assert r.status_code == 401, r.text
    body = r.json()
    assert body["error"] == "invalid_token"


def test_acme_token_at_gdx_mcp_returns_401(app_client: TestClient) -> None:
    """Symmetric: cross-tenant denial works either direction."""
    token = _mint(ACME_UUID, ACME_HOST)
    r = _post_init(app_client, GDX_HOST, token)
    assert r.status_code == 401, r.text


# ── missing / malformed auth ────────────────────────────────────────────────


def test_no_bearer_returns_401(app_client: TestClient) -> None:
    r = _post_init(app_client, GDX_HOST, token=None)
    assert r.status_code == 401, r.text
    body = r.json()
    assert body["error"] == "invalid_token"
    assert "missing bearer" in body["error_description"].lower()


def test_malformed_bearer_returns_401(app_client: TestClient) -> None:
    r = _post_init(app_client, GDX_HOST, token="not-a-jwt")
    assert r.status_code == 401, r.text
    assert r.headers.get("WWW-Authenticate", "").startswith("Bearer ")


def test_unknown_tenant_host_is_denied(app_client: TestClient) -> None:
    """Unknown host must never 200. Which layer rejects depends on setup:
    TenantMiddleware 404s (`Unknown tenant`), the bearer middleware 401s
    (issuer mismatch → invalid_token per RFC 6750), or 403 when the
    tenant resolves but the transport refuses the binding."""
    token = _mint(GDX_UUID, GDX_HOST)
    headers = {"Host": "stranger.example.com",
               "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {token}"}
    r = app_client.post("/mcp/", json=_initialize_payload(), headers=headers,
                        follow_redirects=True)
    assert r.status_code in (401, 403, 404), r.text
    assert r.status_code != 200


# ── WWW-Authenticate per RFC 6750 ───────────────────────────────────────────


def test_failed_auth_includes_www_authenticate_challenge(app_client: TestClient) -> None:
    """RFC 6750 §3 — 401/403 from a Bearer-protected resource MUST set
    WWW-Authenticate. claude.ai's connector reads this for error UX."""
    r = _post_init(app_client, GDX_HOST, token=None)
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers
    challenge = r.headers["WWW-Authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'realm="mcp"' in challenge
    assert 'error="invalid_token"' in challenge
