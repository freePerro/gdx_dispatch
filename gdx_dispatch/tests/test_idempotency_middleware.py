"""Tests for IdempotencyMiddleware (SS-14 slice B).

Uses an in-memory fake Redis with deliberate hit/miss paths plus
explicit failure injections, so the fail-open semantics are exercised.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.datastructures import State
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from gdx_dispatch.core.middleware.idempotency import IdempotencyMiddleware
from gdx_dispatch.core.middleware.idempotency_keys import build_cache_key


class FakeRedis:
    """Minimal in-memory stand-in for redis-py supporting get/setex."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.fail_get = False
        self.fail_setex = False
        self.setex_calls: list[tuple[str, int, str]] = []

    def get(self, key: str):
        if self.fail_get:
            # Realistic redis-py transport failure shape. The middleware
            # narrows its except to transport-family errors; a bare
            # RuntimeError would (correctly) propagate and break the
            # request — use ConnectionError to mirror actual prod behavior.
            raise ConnectionError("redis_unavailable")
        return self.store.get(key)

    def setex(self, key: str, ttl: int, value: str) -> None:
        if self.fail_setex:
            # Realistic redis-py transport failure shape. The middleware
            # narrows its except to transport-family errors; a bare
            # RuntimeError would (correctly) propagate and break the
            # request — use ConnectionError to mirror actual prod behavior.
            raise ConnectionError("redis_unavailable")
        self.setex_calls.append((key, ttl, value))
        self.store[key] = value


def _build_app(redis_client, principal, counter, response_fn=None):
    async def endpoint(request: Request):
        # Attach principal BEFORE the middleware sees the request:
        # Starlette dispatches middleware outermost first, so set it via
        # a wrapper below via request.state assignment happens in route;
        # BUT we need principal in place BEFORE the middleware dispatch.
        # Instead we attach it via a tiny ASGI wrapper (auth_wrapper).
        counter["hits"] += 1
        if response_fn:
            return response_fn()
        return JSONResponse({"ok": True, "n": counter["hits"]}, status_code=201)

    async def auth_wrapper(scope, receive, send):
        if scope["type"] == "http":
            # Starlette's Request.state reads scope["state"] via attribute
            # access. Put a real State() there so request.state.principal
            # resolves during middleware dispatch.
            state = State()
            if principal is not None:
                state.principal = principal
            scope["state"] = state
        await app_inner(scope, receive, send)

    app_inner = Starlette(
        routes=[Route("/v1/thing", endpoint, methods=["POST", "GET"])],
        middleware=[Middleware(IdempotencyMiddleware, redis_client=redis_client)],
    )
    return auth_wrapper


def _principal(tenant_id="tenant-a", identity_id="identity-1"):
    return SimpleNamespace(tenant_id=tenant_id, identity_id=identity_id)


def test_non_post_skips_middleware():
    """GET requests pass straight through; nothing cached."""
    r = FakeRedis()
    counter = {"hits": 0}
    app = _build_app(r, _principal(), counter)
    client = TestClient(app)
    resp = client.get("/v1/thing", headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 200 or resp.status_code == 201  # GET handler returns 201 too
    assert counter["hits"] == 1
    assert r.setex_calls == []


def test_post_without_key_skips_cache():
    """POST without Idempotency-Key header: no cache interaction."""
    r = FakeRedis()
    counter = {"hits": 0}
    app = _build_app(r, _principal(), counter)
    client = TestClient(app)
    resp = client.post("/v1/thing")
    assert resp.status_code == 201
    assert counter["hits"] == 1
    assert r.setex_calls == []


def test_post_without_principal_skips_cache():
    """POST with Idempotency-Key but no principal: pass-through."""
    r = FakeRedis()
    counter = {"hits": 0}
    app = _build_app(r, None, counter)
    client = TestClient(app)
    resp = client.post("/v1/thing", headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 201
    assert counter["hits"] == 1
    assert r.setex_calls == []


def test_first_post_stores_and_replay_returns_cached():
    """First POST caches; replay with same key returns cached body without calling handler."""
    r = FakeRedis()
    counter = {"hits": 0}
    app = _build_app(r, _principal(), counter)
    client = TestClient(app)

    first = client.post("/v1/thing", headers={"Idempotency-Key": "k-abc"})
    assert first.status_code == 201
    assert counter["hits"] == 1
    assert len(r.setex_calls) == 1

    expected_key = build_cache_key("tenant-a", "identity-1", "k-abc", "/v1/thing")
    assert r.setex_calls[0][0] == expected_key

    second = client.post("/v1/thing", headers={"Idempotency-Key": "k-abc"})
    assert second.status_code == 201
    assert second.json() == first.json()
    # Handler was NOT called a second time.
    assert counter["hits"] == 1


def test_non_2xx_response_is_not_cached():
    """4xx/5xx responses are passed through but not stored."""
    r = FakeRedis()
    counter = {"hits": 0}
    app = _build_app(
        r,
        _principal(),
        counter,
        response_fn=lambda: JSONResponse({"error": "nope"}, status_code=400),
    )
    client = TestClient(app)
    resp = client.post("/v1/thing", headers={"Idempotency-Key": "k-bad"})
    assert resp.status_code == 400
    assert r.setex_calls == []


def test_non_json_body_not_cached_but_returned():
    """Plain-text 2xx response: not cached (can't roundtrip) but returned."""
    r = FakeRedis()
    counter = {"hits": 0}
    app = _build_app(
        r,
        _principal(),
        counter,
        response_fn=lambda: PlainTextResponse("hello", status_code=200),
    )
    client = TestClient(app)
    resp = client.post("/v1/thing", headers={"Idempotency-Key": "k-text"})
    assert resp.status_code == 200
    assert resp.text == "hello"
    assert r.setex_calls == []


def test_redis_get_failure_falls_through_to_handler():
    """Fail-open on Redis GET error: handler runs, response returned."""
    r = FakeRedis()
    r.fail_get = True
    counter = {"hits": 0}
    app = _build_app(r, _principal(), counter)
    client = TestClient(app)
    resp = client.post("/v1/thing", headers={"Idempotency-Key": "k-fg"})
    assert resp.status_code == 201
    assert counter["hits"] == 1


def test_redis_setex_failure_still_returns_response():
    """Fail-open on Redis SETEX error: response still returned normally."""
    r = FakeRedis()
    r.fail_setex = True
    counter = {"hits": 0}
    app = _build_app(r, _principal(), counter)
    client = TestClient(app)
    resp = client.post("/v1/thing", headers={"Idempotency-Key": "k-fs"})
    assert resp.status_code == 201
    assert resp.json()["ok"] is True


def test_cache_key_isolation_across_tenants():
    """Same key from different tenants must NOT collide."""
    r = FakeRedis()
    counter_a = {"hits": 0}
    counter_b = {"hits": 0}
    app_a = _build_app(r, _principal(tenant_id="tenant-a"), counter_a)
    app_b = _build_app(r, _principal(tenant_id="tenant-b"), counter_b)

    TestClient(app_a).post("/v1/thing", headers={"Idempotency-Key": "same"})
    TestClient(app_b).post("/v1/thing", headers={"Idempotency-Key": "same"})

    # Both handlers ran once — no cross-tenant cache hit.
    assert counter_a["hits"] == 1
    assert counter_b["hits"] == 1
    assert len(r.setex_calls) == 2


def test_cached_payload_shape_roundtrips():
    """Cached JSON has status + body keys and replays exactly."""
    r = FakeRedis()
    counter = {"hits": 0}
    app = _build_app(r, _principal(), counter)
    client = TestClient(app)
    first = client.post("/v1/thing", headers={"Idempotency-Key": "k-rt"})
    assert first.status_code == 201

    stored = json.loads(r.setex_calls[0][2])
    assert stored["status"] == 201
    assert stored["body"] == first.json()
