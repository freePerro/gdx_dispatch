"""SS-26 Slice B: /.well-known/* router tests."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.routers.well_known import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_oauth_authorization_server_document_is_gone():
    """RFC 8414 metadata described endpoints that no longer exist.

    Serving it sent a conforming client to /oauth/authorize, which answers
    with the SPA's HTML on production. No document is better than a document
    that leads nowhere.
    """
    assert _client().get("/.well-known/oauth-authorization-server").status_code == 404


def test_openid_configuration_document_is_gone():
    assert _client().get("/.well-known/openid-configuration").status_code == 404


def test_protected_resource_metadata_document_is_gone():
    """MCP Authorization 2025-06-18 requires a PRM to name at least one
    authorization server. This server has none, so it serves no PRM rather
    than a non-conformant one or one pointing at deleted metadata."""
    c = _client()
    assert c.get("/.well-known/oauth-protected-resource").status_code == 404
    assert c.get("/.well-known/oauth-protected-resource/mcp").status_code == 404


def test_gdx_platform_manifest_returns_json_and_links_all_endpoints():
    r = _client().get("/.well-known/gdx-platform")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    data = r.json()
    assert data["name"] == "GDX Platform"
    assert data["version"]
    assert "directory_endpoints" in data
    for key in (
        "gdx_platform",
        "security_txt",
        "mcp_tools",
    ):
        assert key in data["directory_endpoints"]
    for gone in ("oauth_authorization_server", "openid_configuration",
                 "oauth_protected_resource"):
        assert gone not in data["directory_endpoints"]


def test_security_txt_returns_plain_text_with_required_rfc9116_fields():
    r = _client().get("/.well-known/security.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "Contact:" in body
    assert "Expires:" in body
    assert "Preferred-Languages:" in body


def test_mcp_tools_returns_json_with_tool_entries():
    r = _client().get("/.well-known/mcp-tools")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    data = r.json()
    # Deliberately no tool inventory: this document is unauthenticated.
    assert "tools" not in data
    # mcp_endpoint is the Streamable-HTTP transport. legacy_mcp_endpoint and
    # tools_index_url are gone: /api/mcp and /api/mcp/tools have no routes.
    assert data["mcp_endpoint"].endswith("/mcp")
    assert "legacy_mcp_endpoint" not in data
    assert "tools_index_url" not in data


def test_all_endpoints_respond_successfully_from_platform_directory():
    """Agent discovery flow: start at gdx-platform manifest,
    follow every directory_endpoints link, each must 200."""
    client = _client()
    r = client.get("/.well-known/gdx-platform")
    data = r.json()
    for name, url in data["directory_endpoints"].items():
        # gdx_platform points at itself; others are the absolute public URL.
        # Tests exercise only the local path portion.
        if "://" in url:
            path = "/" + url.split("/", 3)[-1] if url.count("/") >= 3 else url
        else:
            path = url
        resp = client.get(path)
        assert resp.status_code == 200, f"{name} {path} -> {resp.status_code}"
