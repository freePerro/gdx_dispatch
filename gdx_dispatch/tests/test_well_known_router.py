"""SS-26 Slice B: /.well-known/* router tests."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.routers.well_known import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_oauth_authorization_server_returns_json_with_required_fields():
    r = _client().get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    data = r.json()
    for field in (
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "revocation_endpoint",
        "introspection_endpoint",
        "grant_types_supported",
        "code_challenge_methods_supported",
    ):
        assert field in data, f"missing {field}"
    assert "S256" in data["code_challenge_methods_supported"]


def test_openid_configuration_returns_json():
    r = _client().get("/.well-known/openid-configuration")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    data = r.json()
    assert data["issuer"]
    assert "RS256" in data["id_token_signing_alg_values_supported"]
    assert "S256" in data["code_challenge_methods_supported"]


def test_gdx_platform_manifest_returns_json_and_links_all_endpoints():
    r = _client().get("/.well-known/gdx-platform")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    data = r.json()
    assert data["name"] == "GDX Platform"
    assert data["version"]
    assert "directory_endpoints" in data
    for key in (
        "oauth_authorization_server",
        "openid_configuration",
        "gdx_platform",
        "security_txt",
        "mcp_tools",
    ):
        assert key in data["directory_endpoints"]


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
    assert "tools" in data
    assert isinstance(data["tools"], list)
    # Sprint mcp-streamable-http S3: mcp_endpoint now points at the
    # Streamable-HTTP transport (/mcp). The legacy /api/mcp path is
    # still discoverable via legacy_mcp_endpoint.
    assert data["mcp_endpoint"].endswith("/mcp")
    assert data["legacy_mcp_endpoint"].endswith("/api/mcp")


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
