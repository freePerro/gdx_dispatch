"""
Tests for PWA service worker, manifest, and push notification endpoints.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.tests.conftest import app_route_paths


def _pwa_client() -> TestClient:
    """Minimal app with only the PWA router — no TenantMiddleware."""
    from gdx_dispatch.core.pwa import PWARouter

    app = FastAPI()
    app.include_router(PWARouter)
    return TestClient(app, raise_server_exceptions=False)


def _push_client(tenant_id: str = "tenant-pwa-test") -> TestClient:
    """Minimal app with only the push notifications router + tenant middleware."""
    from gdx_dispatch.core.push_notifications import router as push_router

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": tenant_id}
        return await call_next(request)

    app.include_router(push_router)
    return TestClient(app, raise_server_exceptions=False)


# ── 1: sw.js endpoint returns 200 with JS content type ────────────────────

def test_sw_js_endpoint_200():
    """GET /sw.js must return HTTP 200 with application/javascript content type."""
    client = _pwa_client()
    resp = client.get("/sw.js")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert "javascript" in resp.headers.get("content-type", ""), (
        f"Expected JS content type, got {resp.headers.get('content-type')}"
    )


# ── 2: manifest.json returns valid JSON ────────────────────────────────────

def test_manifest_json_valid():
    """GET /manifest.json must return valid JSON with required PWA fields."""
    client = _pwa_client()
    resp = client.get("/manifest.json")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    data = resp.json()
    assert "name" in data, "manifest missing 'name'"
    assert "short_name" in data, "manifest missing 'short_name'"
    assert "start_url" in data, "manifest missing 'start_url'"
    assert "display" in data, "manifest missing 'display'"
    assert data["display"] == "standalone"
    assert data["short_name"] == "GDX"


# ── 3: push subscribe endpoint ─────────────────────────────────────────────

def test_push_subscribe():
    """POST /api/push/subscribe must accept a subscription and return status=subscribed."""
    from gdx_dispatch.core.push_notifications import _subscriptions

    _subscriptions.clear()

    tenant_id = "tenant-pwa-test"
    client = _push_client(tenant_id)
    payload = {
        "endpoint": "https://push.example.com/endpoint-abc",
        "p256dh": "AAAA1234p256dhkey",
        "auth": "authkey123",
    }
    resp = client.post("/api/push/subscribe", json=payload)
    assert resp.status_code in (200, 201), f"Expected 200/201, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data.get("status") == "subscribed", f"Expected status=subscribed, got {data}"
    assert "https://push.example.com/endpoint-abc" in _subscriptions.get(tenant_id, {})


# ── 4: push unsubscribe endpoint ───────────────────────────────────────────

def test_push_unsubscribe():
    """DELETE /api/push/unsubscribe must remove a subscription."""
    from gdx_dispatch.core.push_notifications import _subscriptions

    _subscriptions.clear()
    tenant_id = "tenant-pwa-test"
    endpoint = "https://push.example.com/endpoint-to-remove"
    _subscriptions[tenant_id] = {endpoint: {"endpoint": endpoint, "p256dh": "key", "auth": "auth"}}

    client = _push_client(tenant_id)
    resp = client.request("DELETE", "/api/push/unsubscribe", json={"endpoint": endpoint})
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data.get("status") == "unsubscribed", f"Expected status=unsubscribed, got {data}"
    assert endpoint not in _subscriptions.get(tenant_id, {})


# ── 5: send notification (mock pywebpush) ─────────────────────────────────

def test_send_notification_mock(monkeypatch):
    """POST /api/push/send must call send_push_notification for each subscriber."""
    import gdx_dispatch.core.push_notifications as pn

    pn._subscriptions.clear()
    tenant_id = "tenant-pwa-test"
    pn._subscriptions[tenant_id] = {
        "https://push.example.com/ep1": {
            "endpoint": "https://push.example.com/ep1",
            "p256dh": "key1",
            "auth": "auth1",
        },
    }

    calls: list[dict] = []

    def mock_send(sub_info: dict, payload: dict) -> bool:
        calls.append({"sub_info": sub_info, "payload": payload})
        return True

    monkeypatch.setattr(pn, "send_push_notification", mock_send)

    client = _push_client(tenant_id)
    resp = client.post("/api/push/send", json={
        "title": "Test Notification",
        "body": "Job #42 assigned",
        "url": "/jobs/42",
    })
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data.get("status") == "sent"
    assert data.get("sent") == 1
    assert len(calls) == 1
    assert calls[0]["payload"]["title"] == "Test Notification"


# ── 6: sw.js is a kill-switch (no cache-first, unregisters legacy PWA) ─────

def test_sw_js_is_kill_switch():
    """sw.js must be a kill-switch: unregister self, delete all caches, no fetch handler.

    A prior deploy shipped a cache-first SW that stranded returning visitors on
    stale Vue chunks after every frontend rebuild. The current Vue frontend does
    not register a service worker; /sw.js exists only to dismantle legacy SWs
    still installed in returning visitors' browsers.
    """
    client = _pwa_client()
    resp = client.get("/sw.js")
    assert resp.status_code == 200

    body = resp.text
    assert "self.registration.unregister" in body, (
        "sw.js must call self.registration.unregister() to dismantle legacy PWA"
    )
    assert "caches.delete" in body, "sw.js must delete all caches"
    assert "gdx-v1" not in body, (
        "Legacy 'gdx-v1' cache name must not be present — kill-switch only"
    )
    assert "APP_SHELL" not in body, (
        "APP_SHELL precache list must not be present — kill-switch only"
    )
    assert "addEventListener('fetch'" not in body and 'addEventListener("fetch"' not in body, (
        "sw.js must not register a fetch handler — all requests must pass through to network"
    )

    cache_control = resp.headers.get("cache-control", "")
    assert "no-cache" in cache_control or "no-store" in cache_control, (
        f"sw.js must have no-cache headers, got: {cache_control}"
    )


# ── 7: route registration check ───────────────────────────────────────────

def test_pwa_routes_registered_in_app():
    """App routes must include /sw.js, /manifest.json, and /api/push/subscribe."""
    from gdx_dispatch.app import create_app

    app = create_app()
    paths = app_route_paths(app)
    assert "/sw.js" in paths, f"/sw.js not registered. Found: {paths}"
    assert "/manifest.json" in paths, f"/manifest.json not registered. Found: {paths}"
    assert any("/api/push" in p for p in paths), f"/api/push/* not registered. Found: {paths}"

def test_push_send_does_not_cross_tenants(monkeypatch):
    """Regression: send_to_all must never deliver a tenant A subscription to tenant B.

    Before 2026-04-19 the module-level _subscriptions dict was keyed only by
    endpoint URL, so calling /api/push/send from tenant A would broadcast to
    every tenant's subscriptions.
    """
    import gdx_dispatch.core.push_notifications as pn

    pn._subscriptions.clear()
    # Seed subscriptions for two different tenants, same endpoint style
    pn._subscriptions["tenant-alpha"] = {
        "https://push.example.com/alpha": {"endpoint": "https://push.example.com/alpha", "p256dh": "a", "auth": "a"},
    }
    pn._subscriptions["tenant-beta"] = {
        "https://push.example.com/beta": {"endpoint": "https://push.example.com/beta", "p256dh": "b", "auth": "b"},
    }

    delivered: list[str] = []

    def mock_send(sub_info, payload):
        delivered.append(sub_info["endpoint"])
        return True

    monkeypatch.setattr(pn, "send_push_notification", mock_send)

    # Caller is tenant-alpha — only alpha's subscriber should receive
    client = _push_client("tenant-alpha")
    resp = client.post("/api/push/send", json={"title": "x", "body": "y"})
    assert resp.status_code == 200
    assert delivered == ["https://push.example.com/alpha"], (
        f"push_notifications leaked across tenants: delivered={delivered}"
    )
