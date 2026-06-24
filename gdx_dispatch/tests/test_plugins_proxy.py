"""Tests for the core→plugin-host proxy (ADR-013 step 3b): identity forwarding
and anti-spoofing. Mocks the upstream httpx call. Needs FastAPI → docker image.
"""
from __future__ import annotations

from fastapi import FastAPI
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


def _client(monkeypatch, *, modules):
    monkeypatch.setattr(plugins_proxy.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(plugins_proxy, "enabled_module_keys", lambda db, tid: set(modules))

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
