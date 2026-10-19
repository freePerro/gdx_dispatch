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
from typing import Annotated

import jwt
from fastapi import Depends, FastAPI
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from gdx_dispatch.core.middleware.idempotency import IdempotencyMiddleware
from gdx_dispatch.core.middleware.principal_stamp import PrincipalStampMiddleware
from gdx_dispatch.routers.auth.core import get_current_user

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
    bad = _token(secret=SECRET + "-wrong")
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


def _authed_app(redis):
    """The route enforces get_current_user, as every real money route does —
    so a replay that skips it is visible as a 200 where a 401 belongs."""
    calls = {"n": 0}
    app = FastAPI()
    # add_middleware wraps outermost-last: the stamp must sit outside the cache.
    app.add_middleware(IdempotencyMiddleware, redis_client=redis)
    app.add_middleware(PrincipalStampMiddleware)

    @app.post("/api/pay")
    async def pay(user: Annotated[dict, Depends(get_current_user)]):
        calls["n"] += 1
        return {"created": calls["n"]}

    return app, calls


def _jti_token(jti, sub="user-1"):
    return jwt.encode({"sub": sub, "tenant_id": "tenant-m36", "typ": "access", "jti": jti},
                      SECRET, algorithm="HS256")


def _revoke(app, jti):
    from datetime import datetime, timedelta, timezone

    from gdx_dispatch.core.denylist import Denylist

    if getattr(app.state, "denylist", None) is None:
        app.state.denylist = Denylist(redis_client=None)
    app.state.denylist.add(jti, datetime.now(timezone.utc) + timedelta(hours=1))


def test_replay_after_revoke_is_401_not_the_cached_body():
    """GDXA-1302: the stamp must apply the denylist. Before the fix the
    revoked token's replay was answered from cache with 200 {"created": 1}
    without get_current_user ever running."""
    redis = FakeRedis()
    app, calls = _authed_app(redis)
    client = TestClient(app)
    tok = _jti_token("jti-revoke-me")
    r1 = _post(client, key="k1", token=tok)
    assert r1.status_code == 200 and r1.json() == {"created": 1}
    assert redis.store, "the first call must have been cached for this test to mean anything"

    _revoke(app, "jti-revoke-me")
    r2 = _post(client, key="k1", token=tok)
    assert r2.status_code == 401, f"revoked token got {r2.status_code} {r2.text}"
    assert calls["n"] == 1


def test_replay_by_a_deactivated_user_is_not_served_from_cache(monkeypatch):
    """The DB-verify gate (user missing/deleted/inactive) applies too. The
    lookup itself is stubbed — the test app has no tenant database — but the
    real finalize_login_jwt and the real route dependency run."""
    import gdx_dispatch.routers.auth.core as auth_core

    redis = FakeRedis()
    app, calls = _authed_app(redis)
    client = TestClient(app)
    tok = _jti_token("jti-live")
    assert _post(client, key="k1", token=tok).status_code == 200

    monkeypatch.setattr(auth_core, "_db_verify_user", lambda *a, **k: None)
    r2 = _post(client, key="k1", token=tok)
    assert r2.status_code == 401, f"deactivated user got {r2.status_code} {r2.text}"
    assert calls["n"] == 1


def test_a_lookup_blip_in_the_stamp_never_runs_the_handler_twice(monkeypatch):
    """The user lookup cannot tell "not eligible" from "database errored"
    (both return None). If the stamp merely skipped stamping on a blip, the
    route's own lookup a moment later could succeed and run the handler a
    second time — on an offline replay, the duplicate the cache exists to
    stop. The stamp answers the 401 itself instead; the client retries."""
    import gdx_dispatch.routers.auth.core as auth_core

    redis = FakeRedis()
    app, calls = _authed_app(redis)
    client = TestClient(app)
    tok = _jti_token("jti-live")
    assert _post(client, key="k1", token=tok).status_code == 200

    real = auth_core._db_verify_user
    blips = {"left": 1}

    def flaky(*a, **k):
        if blips["left"]:
            blips["left"] -= 1
            return None
        return real(*a, **k)

    monkeypatch.setattr(auth_core, "_db_verify_user", flaky)
    r2 = _post(client, key="k1", token=tok)
    assert r2.status_code == 401
    assert calls["n"] == 1, "the blip let the replay reach the handler"
    r3 = _post(client, key="k1", token=tok)
    assert r3.status_code == 200 and r3.json() == {"created": 1}
    assert calls["n"] == 1


def test_a_403_gate_passes_through_for_the_route_to_answer():
    """Only the 401 gates are answered by the stamp. A tenant mismatch is
    the route's 403 to give, and nothing is cached."""
    from starlette.middleware.base import BaseHTTPMiddleware

    class WrongHost(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.tenant = {"id": "some-other-tenant"}
            return await call_next(request)

    redis = FakeRedis()
    app, calls = _authed_app(redis)
    app.add_middleware(WrongHost)  # outermost, like TenantMiddleware
    client = TestClient(app)
    r = _post(client, key="k1", token=_jti_token("jti-live"))
    assert r.status_code == 403, f"{r.status_code} {r.text}"
    assert "tenant" in r.text.lower()
    assert calls["n"] == 0 and redis.store == {}


def test_live_token_with_jti_still_replays_from_cache():
    """The gates must not switch the cache off for a valid caller."""
    redis = FakeRedis()
    app, calls = _authed_app(redis)
    client = TestClient(app)
    tok = _jti_token("jti-live")
    r1 = _post(client, key="k1", token=tok)
    r2 = _post(client, key="k1", token=tok)
    assert r1.status_code == r2.status_code == 200
    assert calls["n"] == 1 and r2.json() == {"created": 1}


def test_stamp_does_not_leave_request_state_user_behind():
    """finalize_login_jwt stashes request.state.user; the stamp must undo
    that, so a route that never authenticates does not see a user."""
    redis = FakeRedis()
    seen = {}

    async def endpoint(request: Request):
        seen["user"] = getattr(request.state, "user", "absent")
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[Route("/api/pay", endpoint, methods=["POST"])],
        middleware=[
            Middleware(PrincipalStampMiddleware),
            Middleware(IdempotencyMiddleware, redis_client=redis),
        ],
    )
    client = TestClient(app)
    assert _post(client, key="k1", token=_jti_token("jti-x")).status_code == 200
    assert seen["user"] == "absent"
    assert redis.store, "the token was valid, so the stamp should still have engaged"


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
