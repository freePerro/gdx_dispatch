"""The idempotency replay cache actually functions (M36).

The SS-14 middleware was registered but permanently pass-through: it requires
``request.state.principal`` and nothing in production ever stamped it — while
the offline sync queue sent ``Idempotency-Key`` on every replay. These tests
drive the REAL two-middleware stack (PrincipalStampMiddleware outside,
IdempotencyMiddleware inside) with a real signed HS256 token and prove the
second identical POST is served from cache without reaching the handler.
"""
from __future__ import annotations

import os

import jwt
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from gdx_dispatch.core.middleware.idempotency import IdempotencyMiddleware
from gdx_dispatch.core.middleware.principal_stamp import PrincipalStampMiddleware

SECRET = os.environ["JWT_SECRET"]


class FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


def _token(sub="user-1", tenant="tenant-m36", typ="access", secret=None):
    return jwt.encode({"sub": sub, "tenant_id": tenant, "typ": typ},
                      secret or SECRET, algorithm="HS256")


def _app(redis):
    calls = {"n": 0}

    async def endpoint(request: Request):
        calls["n"] += 1
        return JSONResponse({"created": calls["n"]})

    app = Starlette(
        routes=[Route("/api/pay", endpoint, methods=["POST", "GET"])],
        middleware=[
            # Starlette applies list order outermost-first: the stamp must
            # run before the cache reads request.state.principal.
            Middleware(PrincipalStampMiddleware),
            Middleware(IdempotencyMiddleware, redis_client=redis),
        ],
    )
    return app, calls


def _post(client, key=None, token=None):
    headers = {}
    if key:
        headers["Idempotency-Key"] = key
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/api/pay", headers=headers, json={"amount": 500})


def test_replay_is_served_from_cache_not_the_handler():
    """THE FIX: same key + same verified caller -> the handler runs ONCE and
    the replay gets the identical cached body."""
    redis = FakeRedis()
    app, calls = _app(redis)
    client = TestClient(app)
    tok = _token()
    r1 = _post(client, key="k-1", token=tok)
    r2 = _post(client, key="k-1", token=tok)
    assert r1.status_code == 200 and r2.status_code == 200
    assert calls["n"] == 1, "the replay reached the handler — cache inert"
    assert r1.json() == r2.json() == {"created": 1}


def test_a_different_key_is_a_new_request():
    redis = FakeRedis()
    app, calls = _app(redis)
    client = TestClient(app)
    tok = _token()
    _post(client, key="k-1", token=tok)
    _post(client, key="k-2", token=tok)
    assert calls["n"] == 2


def test_unverifiable_token_stamps_nothing_and_passes_through():
    """A garbage/wrong-key token must NOT mint a cache identity (cache keys
    from attacker-writable claims) — and must NOT block the request either:
    auth stays the route's job."""
    redis = FakeRedis()
    app, calls = _app(redis)
    client = TestClient(app)
    bad = _token(secret="w" * 40)
    r1 = _post(client, key="k-1", token=bad)
    r2 = _post(client, key="k-1", token=bad)
    assert r1.status_code == 200 and r2.status_code == 200
    assert calls["n"] == 2, "an unverified token must not be replay-cached"
    assert redis.store == {}


def test_no_header_means_no_stamp_and_no_cache():
    redis = FakeRedis()
    app, calls = _app(redis)
    client = TestClient(app)
    tok = _token()
    _post(client, token=tok)
    _post(client, token=tok)
    assert calls["n"] == 2
    assert redis.store == {}


def test_refresh_token_typ_is_refused():
    """Only typ=access (or legacy no-typ) may mint a cache identity."""
    redis = FakeRedis()
    app, calls = _app(redis)
    client = TestClient(app)
    tok = _token(typ="refresh")
    _post(client, key="k-1", token=tok)
    _post(client, key="k-1", token=tok)
    assert calls["n"] == 2 and redis.store == {}


def test_two_callers_with_the_same_key_do_not_collide():
    """The cache key includes the identity — tech A's replay must never
    return tech B's response."""
    redis = FakeRedis()
    app, calls = _app(redis)
    client = TestClient(app)
    _post(client, key="k-1", token=_token(sub="tech-a"))
    r = _post(client, key="k-1", token=_token(sub="tech-b"))
    assert calls["n"] == 2
    assert r.json() == {"created": 2}


def test_an_upstream_stamp_is_never_overwritten():
    """When SS-9 (or a test) stamps a principal first, the shim defers."""
    redis = FakeRedis()
    calls = {"n": 0}

    async def endpoint(request: Request):
        calls["n"] += 1
        return JSONResponse({"who": request.state.principal.identity_id})

    class Pre(PrincipalStampMiddleware):
        pass

    from starlette.middleware.base import BaseHTTPMiddleware

    class Upstream(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            from gdx_dispatch.core.middleware.principal_stamp import StampedPrincipal
            request.state.principal = StampedPrincipal("tenant-x", "richer-identity")
            return await call_next(request)

    app = Starlette(
        routes=[Route("/api/pay", endpoint, methods=["POST"])],
        middleware=[
            Middleware(Upstream),
            Middleware(Pre),
            Middleware(IdempotencyMiddleware, redis_client=redis),
        ],
    )
    client = TestClient(app)
    r = _post(client, key="k-1", token=_token(sub="jwt-identity"))
    assert r.json() == {"who": "richer-identity"}


def test_the_real_app_registers_the_stamp_outside_the_cache(monkeypatch):
    """The unit tests above build their own apps — this pins the PRODUCTION
    registration, so deleting the app.py wiring cannot pass silently. The
    stamp must sit OUTSIDE (before) the SS-14 cache; Starlette's
    user_middleware lists outermost first.

    Audit round 2: the ordering assertion used to hide behind
    `if IdempotencyMiddleware in classes:` — and the test harness has no
    REDIS_URL, so SS-14 was absent and the ordering could never fail. Set a
    REDIS_URL (the client is lazy — no connection happens at create_app) so
    BOTH middlewares register and the order is asserted unconditionally."""
    from gdx_dispatch.core.middleware.idempotency import IdempotencyMiddleware
    from gdx_dispatch.core.middleware.principal_stamp import PrincipalStampMiddleware

    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/7")
    from gdx_dispatch.app import create_app

    app = create_app()
    classes = [m.cls for m in app.user_middleware]
    assert PrincipalStampMiddleware in classes, "M36 stamp not registered on the real app"
    assert IdempotencyMiddleware in classes, (
        "SS-14 cache absent even with REDIS_URL set — the registration broke"
    )
    assert classes.index(PrincipalStampMiddleware) < classes.index(IdempotencyMiddleware), (
        "stamp must run BEFORE the cache reads request.state.principal"
    )
