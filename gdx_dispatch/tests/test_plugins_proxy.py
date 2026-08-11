"""Tests for the core→plugin-host proxy (ADR-013 step 3b): identity forwarding
and anti-spoofing. Mocks the upstream httpx call. Needs FastAPI → docker image.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from gdx_dispatch.routers import plugins_proxy
from gdx_dispatch.routers.auth import get_current_user


class _FakeResp:
    status_code = 200
    content = b'{"ok": true}'
    headers = {"content-type": "application/json"}


class _FakeClient:
    """Stand-in for httpx.AsyncClient that records the forwarded request."""

    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, params=None, content=None, headers=None):
        _FakeClient.captured = {"method": method, "url": url, "headers": headers}
        return _FakeResp()


def _app(monkeypatch, *, modules, perms=("*",)):
    return _client(monkeypatch, modules=modules, perms=perms).app


def _client(monkeypatch, *, modules, perms=("*",)):
    monkeypatch.setattr(plugins_proxy.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(plugins_proxy, "enabled_module_keys", lambda db, tid: set(modules))
    # Per-plugin authorization resolves the caller's permission set; these tests
    # run without a DB, so stand in for the resolver. Default is the owner
    # wildcard so the pre-existing identity-forwarding tests keep their subject.
    monkeypatch.setattr(
        plugins_proxy, "_load_user_permissions", lambda db, request, user: set(perms)
    )

    app = FastAPI()

    @app.middleware("http")
    async def _set_tenant(request, call_next):
        request.state.tenant = {"id": "tenant-42"}
        return await call_next(request)

    app.include_router(plugins_proxy.router)
    app.dependency_overrides[get_current_user] = lambda: {"sub": "user-9", "role": "admin"}
    app.dependency_overrides[plugins_proxy.get_db] = lambda: iter([None])
    return TestClient(app)


def test_proxy_forwards_authoritative_identity(monkeypatch):
    c = _client(monkeypatch, modules={"example", "billing"})
    r = c.get("/api/plugins/example/items")
    assert r.status_code == 200
    h = {k.lower(): v for k, v in _FakeClient.captured["headers"].items()}
    assert h["x-gdx-tenant-id"] == "tenant-42"
    assert h["x-gdx-user-id"] == "user-9"
    assert h["x-gdx-role"] == "admin"
    assert set(h["x-gdx-modules"].split(",")) == {"billing", "example"}
    assert _FakeClient.captured["url"].endswith("/api/plugins/example/items")


def test_proxy_catalog_path_has_no_trailing_slash(monkeypatch):
    # GET /api/plugins (empty sub-path) must forward to .../api/plugins, NOT
    # .../api/plugins/ — plugin-host serves the catalog without a trailing slash
    # and a slash 404s there. (Regression: found in live testing.)
    c = _client(monkeypatch, modules={"example"})
    r = c.get("/api/plugins")
    assert r.status_code == 200
    assert _FakeClient.captured["url"].endswith("/api/plugins")
    assert not _FakeClient.captured["url"].endswith("/api/plugins/")


def test_proxy_strips_client_spoofed_gdx_headers(monkeypatch):
    c = _client(monkeypatch, modules={"billing"})  # 'example' NOT granted
    # Client tries to smuggle itself into the 'example' module.
    r = c.get("/api/plugins/example/items", headers={"X-GDX-Modules": "example,admin-everything"})
    assert r.status_code == 200
    h = {k.lower(): v for k, v in _FakeClient.captured["headers"].items()}
    # Authoritative value wins; the spoofed 'example' is gone.
    assert h["x-gdx-modules"] == "billing"


# ---------------------------------------------------------------------------
# Per-plugin authorization + path validation (2026-08-11 audit)
#
# Before this, /api/plugins/* was gated by authentication alone: `require_module`
# checks the TENANT's module grant, not the user's role, and no plugin router
# reads ctx.role. Any logged-in user could call any plugin route.
#
# Worse, the plugin key and the upstream URL were derived independently, and
# httpx resolves dot segments per RFC 3986 — so a path of
# `chipricing/../../../internal/browser/credentials` left the proxy as
# `http://plugin-host:8000/internal/browser/credentials`, plugin-host's
# owner-and-consent-gated saved-login store.
# ---------------------------------------------------------------------------

TRAVERSALS = [
    "chipricing/../midland/quote-lines",          # sideways into another plugin
    "chipricing/../../../internal/browser/credentials",  # out of /api/plugins entirely
    "chipricing/../../internal/restart",
    "../internal/restart",
    "chipricing/./quotes",                        # single-dot segment
    "chipricing//quotes",                         # empty segment → empty plugin key
]


@pytest.mark.parametrize("path", TRAVERSALS)
def test_clean_subpath_refuses_dot_and_empty_segments(path):
    with pytest.raises(HTTPException) as exc:
        plugins_proxy._clean_subpath(path)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("path", ["chipricing/quotes", "chipricing", "a/b/c/d"])
def test_clean_subpath_passes_ordinary_paths(path):
    assert plugins_proxy._clean_subpath(path) == path


@pytest.mark.parametrize("path", ["", "/", "//"])
def test_clean_subpath_treats_bare_root_as_the_catalog(path):
    # Leading/trailing slashes are stripped before the segment check, so
    # `/api/plugins/` is the catalog, not an empty-segment violation.
    assert plugins_proxy._clean_subpath(path) == ""


async def _raw_asgi_get(app, raw_path: str) -> int:
    """GET `raw_path` with NO client-side normalization, returning the status.

    This has to bypass the test client. httpx — like browsers and like nginx —
    resolves `../` when it builds the URL, so a request made through TestClient
    arrives already collapsed and proves nothing about what the server does with
    a raw path. An attacker is not obliged to use a well-behaved client.
    """
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app({
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", TRAVERSALS)
async def test_running_app_refuses_a_raw_traversal(monkeypatch, path):
    """The end-to-end proof: a raw `../` path reaches the app and is refused
    before anything is forwarded.

    Pre-fix, `chipricing/../../../internal/browser/credentials` left the proxy
    as `http://plugin-host:8000/internal/browser/credentials` because httpx
    resolves dot segments when the upstream URL is built — reaching the
    owner-and-consent-gated saved-login store with only a login.
    """
    _FakeClient.captured = {}
    app = _app(monkeypatch, modules={"chipricing", "midland"})
    status = await _raw_asgi_get(app, f"/api/plugins/{path}")
    assert status == 400, f"{path!r} was not refused"
    assert _FakeClient.captured == {}, f"{path!r} reached plugin-host"


def test_proxy_never_builds_a_url_outside_the_plugin_namespace(monkeypatch):
    # Belt and braces: whatever survives validation must still be under
    # /api/plugins/ once httpx has normalized it.
    c = _client(monkeypatch, modules={"chipricing"})
    r = c.get("/api/plugins/chipricing/quotes")
    assert r.status_code == 200
    assert "/api/plugins/chipricing/quotes" in _FakeClient.captured["url"]
    assert "/internal/" not in _FakeClient.captured["url"]


def test_reserved_browser_prefix_is_not_gradeable_as_a_plugin(monkeypatch):
    # `_browser/*` has its own owner+consent door in browser_proxy, registered
    # ahead of this catch-all. Anything arriving here bypassed that door.
    _FakeClient.captured = {}
    c = _client(monkeypatch, modules={"chipricing"})
    r = c.get("/api/plugins/_browser/credentials")
    assert r.status_code == 404
    assert _FakeClient.captured == {}


def test_catalog_list_stays_open_to_any_authenticated_user(monkeypatch):
    # The nav needs to know which plugins exist; the payload is manifests, not
    # plugin data. A user with NO plugin permission at all still reads it.
    c = _client(monkeypatch, modules={"chipricing"}, perms=("jobs.read_own",))
    r = c.get("/api/plugins")
    assert r.status_code == 200


def test_unpermitted_user_is_refused_a_plugin_route(monkeypatch):
    _FakeClient.captured = {}
    c = _client(monkeypatch, modules={"chipricing"}, perms=("jobs.read_own", "mobile.use"))
    r = c.get("/api/plugins/chipricing/quotes")
    assert r.status_code == 403
    assert _FakeClient.captured == {}


def test_per_plugin_grant_does_not_open_other_plugins(monkeypatch):
    c = _client(monkeypatch, modules={"chipricing", "midland"}, perms=("plugin.chipricing.read",))
    assert c.get("/api/plugins/chipricing/quotes").status_code == 200
    _FakeClient.captured = {}
    r = c.get("/api/plugins/midland/quote-lines")
    assert r.status_code == 403
    assert _FakeClient.captured == {}


def test_read_grant_does_not_confer_write(monkeypatch):
    c = _client(monkeypatch, modules={"chipricing"}, perms=("plugin.chipricing.read",))
    assert c.get("/api/plugins/chipricing/quotes").status_code == 200
    _FakeClient.captured = {}
    r = c.post("/api/plugins/chipricing/capture", json={})
    assert r.status_code == 403
    assert _FakeClient.captured == {}


def test_write_grant_allows_the_mutating_methods(monkeypatch):
    c = _client(
        monkeypatch, modules={"chipricing"},
        perms=("plugin.chipricing.read", "plugin.chipricing.write"),
    )
    assert c.post("/api/plugins/chipricing/capture", json={}).status_code == 200
    assert c.put("/api/plugins/chipricing/settings", json={}).status_code == 200
    assert c.delete("/api/plugins/chipricing/quotes/1").status_code == 200


def test_blanket_grant_covers_every_installed_plugin(monkeypatch):
    # What the builtin admin contract holds — admins must not be locked out of
    # their own tenant's plugins by a scheme they can't see.
    c = _client(monkeypatch, modules={"chipricing", "midland"}, perms=("plugins.read", "plugins.write"))
    assert c.get("/api/plugins/chipricing/quotes").status_code == 200
    assert c.get("/api/plugins/midland/quote-lines").status_code == 200
    assert c.post("/api/plugins/midland/quote-lines", json={}).status_code == 200


def test_owner_wildcard_passes(monkeypatch):
    c = _client(monkeypatch, modules={"chipricing"}, perms=("*",))
    assert c.get("/api/plugins/chipricing/quotes").status_code == 200


# The plugin key must LOOK like a plugin key, not merely fail to match a grant.
# uvicorn decodes the path exactly once (verified against a real uvicorn), so a
# DOUBLE-encoded segment arrives as the literal text `%2e%2e` and a backslash
# segment arrives intact. Neither is a traversal from this process's view, and
# both were previously refused only because no grant happened to match — safety
# by coincidence, and no safety at all for a wildcard holder, who would have had
# the segment forwarded verbatim.
MALFORMED_KEYS = [
    "%2e%2e/%2e%2e/internal/restart",        # what `%252e%252e` decodes to
    "chi\\..\\..\\internal/restart",         # backslash segments
    "CHIPRICING/quotes",                     # plugin keys are lowercase
    "-leading-dash/quotes",
    "chi pricing/quotes",
]


@pytest.mark.parametrize("path", MALFORMED_KEYS)
def test_malformed_plugin_key_is_refused_even_for_the_wildcard(monkeypatch, path):
    _FakeClient.captured = {}
    c = _client(monkeypatch, modules={"chipricing"}, perms=("*",))
    r = c.get(f"/api/plugins/{path}")
    assert r.status_code == 400, f"{path!r} was not refused"
    assert _FakeClient.captured == {}, f"{path!r} reached plugin-host"


def test_forwarded_url_never_leaves_the_plugin_namespace(monkeypatch):
    # Whatever survives every check above must still address a plugin route.
    c = _client(monkeypatch, modules={"chipricing"}, perms=("*",))
    for path in ("chipricing", "chipricing/quotes", "chipricing/quotes/1/estimate-line"):
        assert c.get(f"/api/plugins/{path}").status_code == 200
        url = _FakeClient.captured["url"]
        assert url.startswith("http://plugin-host:8000/api/plugins/"), url
        assert "/internal/" not in url
