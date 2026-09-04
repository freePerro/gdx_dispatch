"""`.well-known/*` answers are derived from the inbound host.

The original bug: every answer hard-coded ``issuer = https://gdx.example.com``
regardless of which host received the request, so a client that verifies the
issuer equals the host it asked would reject the document.

The OAuth authorization-server and OpenID documents these tests originally
covered have been removed — this deployment has no authorization server, and
serving metadata for one sent clients to /oauth/* endpoints that answer with
the SPA's HTML. The host-derivation behaviour still matters for the documents
that remain, so the coverage moved rather than went away.
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


def test_manifest_issuer_equals_request_host():
    data = _hit(_client(), "/.well-known/gdx-platform", "gdx.example.com")
    assert data["issuer"] == "http://gdx.example.com", data
    assert data["mcp_endpoint"] == "http://gdx.example.com/mcp"


def test_issuer_follows_a_different_host_with_no_cross_talk():
    """Same code path, different host → different issuer."""
    data = _hit(_client(), "/.well-known/gdx-platform", "acme.example.com")
    assert data["issuer"] == "http://acme.example.com"
    assert "gdx.example.com" not in str(data), (
        "leaked another host into this host's metadata"
    )


def test_removed_documents_are_not_served():
    c = _client()
    assert c.get("/.well-known/oauth-authorization-server",
                 headers={"Host": "gdx.example.com"}).status_code == 404
    assert c.get("/.well-known/openid-configuration",
                 headers={"Host": "gdx.example.com"}).status_code == 404
    assert c.get("/.well-known/oauth-protected-resource",
                 headers={"Host": "gdx.example.com"}).status_code == 404


def test_gdx_platform_manifest_per_tenant_endpoints():
    data = _hit(_client(), "/.well-known/gdx-platform", "gdx.example.com")
    assert data["issuer"] == "http://gdx.example.com"
    assert data["mcp_endpoint"] == "http://gdx.example.com/mcp"
    de = data["directory_endpoints"]
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
        "/.well-known/gdx-platform",
        headers={
            "Host": "gdx.example.com",
            "X-Forwarded-Proto": "https",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["issuer"] == "https://gdx.example.com"
    assert data["mcp_endpoint"] == "https://gdx.example.com/mcp"
    for v in data["directory_endpoints"].values():
        assert v.startswith("https://gdx.example.com/"), v
