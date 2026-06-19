"""Sprint MCP-Streamable-HTTP S3 — per-tenant `.well-known/*` issuer.

The pre-S3 bug: every `.well-known/oauth-authorization-server` answer
hard-coded ``issuer = https://gdx.example.com`` regardless of
which tenant host received the request. claude.ai's MCP connector
fetches OAuth metadata from the tenant host (``gdx.example.com``)
and verifies that the ``issuer`` field equals the host it asked. The
mismatch broke the connector before token issuance even started.

After S3 the issuer equals the inbound host, every endpoint URL
points back at the same host, and the resource indicator in
``oauth-protected-resource`` advertises ``<host>/mcp`` so claude.ai
can wire token ``aud`` → MCP transport without ambiguity.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.routers.well_known import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _hit(client: TestClient, path: str, host: str) -> dict:
    r = client.get(path, headers={"Host": host})
    assert r.status_code == 200, f"{path} on {host}: {r.status_code} {r.text}"
    return r.json()


# ── per-tenant issuer correctness ───────────────────────────────────────────


def test_oauth_authorization_server_issuer_equals_request_host_gdx():
    data = _hit(_client(), "/.well-known/oauth-authorization-server", "gdx.example.com")
    assert data["issuer"] == "http://gdx.example.com", data
    assert data["authorization_endpoint"] == "http://gdx.example.com/oauth/authorize"
    assert data["token_endpoint"] == "http://gdx.example.com/oauth/token"
    assert data["registration_endpoint"] == "http://gdx.example.com/oauth/register"


def test_oauth_authorization_server_issuer_equals_request_host_other_tenant():
    """Same code path, different host → different issuer. No tenant cross-talk."""
    data = _hit(_client(), "/.well-known/oauth-authorization-server", "acme.example.com")
    assert data["issuer"] == "http://acme.example.com"
    assert "gdx_dispatch.example.com" not in str(data), (
        "leaked the platform host into a tenant's metadata"
    )


def test_oauth_authorization_server_advertises_resource_indicators():
    """RFC 8707 — claude.ai's MCP connector requires this to be true."""
    data = _hit(_client(), "/.well-known/oauth-authorization-server", "gdx.example.com")
    assert data.get("resource_indicators_supported") is True


def test_oauth_protected_resource_points_at_tenant_mcp_endpoint():
    """RFC 9728 — `resource` MUST equal the canonical aud claim S4 will mint."""
    data = _hit(_client(), "/.well-known/oauth-protected-resource", "gdx.example.com")
    assert data["resource"] == "http://gdx.example.com/mcp"
    assert data["authorization_servers"] == ["http://gdx.example.com"]
    assert "mcp:invoke" in data["scopes_supported"]


def test_openid_configuration_issuer_is_tenant_scoped():
    data = _hit(_client(), "/.well-known/openid-configuration", "gdx.example.com")
    assert data["issuer"] == "http://gdx.example.com"
    assert data["authorization_endpoint"].startswith("http://gdx.example.com/")


def test_gdx_platform_manifest_per_tenant_endpoints():
    data = _hit(_client(), "/.well-known/gdx-platform", "gdx.example.com")
    assert data["issuer"] == "http://gdx.example.com"
    assert data["mcp_endpoint"] == "http://gdx.example.com/mcp"
    de = data["directory_endpoints"]
    assert de["oauth_protected_resource"] == (
        "http://gdx.example.com/.well-known/oauth-protected-resource"
    )
    for v in de.values():
        assert v.startswith("http://gdx.example.com/"), (
            f"directory link leaked another host: {v}"
        )


# ── X-Forwarded-Proto handling ──────────────────────────────────────────────


def test_x_forwarded_proto_https_is_honored():
    """In production, Cloudflare/nginx terminate TLS and forward via
    X-Forwarded-Proto: https. The issuer must reflect that, not the
    upstream HTTP scheme."""
    r = _client().get(
        "/.well-known/oauth-authorization-server",
        headers={
            "Host": "gdx.example.com",
            "X-Forwarded-Proto": "https",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["issuer"] == "https://gdx.example.com"
    assert data["registration_endpoint"] == "https://gdx.example.com/oauth/register"
