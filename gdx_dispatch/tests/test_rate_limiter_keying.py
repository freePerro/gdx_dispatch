"""Unit tests for the single-tenant per-caller rate-limit keying.

Pre-collapse the middleware keyed its bucket on the request's tenant id. Under
the single-tenant pin that id is a constant, which would make ONE global bucket
for the whole instance (every user + every poll + login sharing it) — an
instance-wide 429 on the first busy minute. These tests pin the replacement
behavior: the bucket is keyed per *caller* (API key / session / IP), and the
unauthenticated auth/signup surface gets a stricter per-IP limit.

These exercise ``_key_and_limit`` directly (pure function of the request) so they
need no Redis — the live limiting is fail-open without Redis anyway.
"""
from __future__ import annotations

from types import SimpleNamespace

from gdx_dispatch.core.rate_limiter import DEFAULT_LIMITS, TenantRateLimitMiddleware


def _mw() -> TenantRateLimitMiddleware:
    # Construct without going through BaseHTTPMiddleware.__init__ (no ASGI app
    # needed to test the pure keying helper).
    return TenantRateLimitMiddleware.__new__(TenantRateLimitMiddleware)


def _req(path: str, headers: dict | None = None, host: str = "1.2.3.4") -> SimpleNamespace:
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        headers=headers or {},
        client=SimpleNamespace(host=host),
    )


def test_auth_paths_keyed_per_ip_with_strict_limit() -> None:
    mw = _mw()
    for path in ("/auth/login", "/auth/login?ref=x", "/portal/login"):
        key, limit = mw._key_and_limit(_req(path))
        assert key == "auth-ip:1.2.3.4", path
        assert limit == DEFAULT_LIMITS["auth"], path  # stricter than general


def test_auth_bucket_is_not_the_anonymous_bucket() -> None:
    """The strict login limit must count login attempts only. Sharing the
    anonymous per-IP key meant the SPA's own asset loads filled it, so a
    customer's first sign-in after a page load or two came back 429
    (found 2026-10-06 walking /customer-portal)."""
    mw = _mw()
    auth_key, _ = mw._key_and_limit(_req("/portal/login"))
    anon_key, _ = mw._key_and_limit(_req("/assets/index.js"))
    assert auth_key != anon_key


def test_retired_signup_path_is_not_an_auth_surface() -> None:
    """``/signup`` was deleted with the SaaS residue (2026-09-01/03). It is now
    just a path the SPA catch-all answers, so it gets the general limit, not
    the brute-force one — the prefix list must not resurrect it."""
    mw = _mw()
    _, limit = mw._key_and_limit(_req("/signup"))
    assert limit != DEFAULT_LIMITS["auth"]


def test_api_key_keyed_per_key_not_per_company() -> None:
    mw = _mw()
    key, limit = mw._key_and_limit(_req("/api/customers", {"x-api-key": "tgd_live_abc"}))
    assert key.startswith("key:")
    assert limit == DEFAULT_LIMITS["general"]
    # Two different keys land in different buckets (no shared global bucket).
    k1, _ = mw._key_and_limit(_req("/api/x", {"x-api-key": "aaa"}))
    k2, _ = mw._key_and_limit(_req("/api/x", {"x-api-key": "bbb"}))
    assert k1 != k2


def test_bearer_keyed_per_session() -> None:
    mw = _mw()
    key, limit = mw._key_and_limit(_req("/api/jobs", {"authorization": "Bearer xyz"}))
    assert key.startswith("sess:")
    assert limit == DEFAULT_LIMITS["general"]


def test_anonymous_falls_back_to_per_ip() -> None:
    mw = _mw()
    key, limit = mw._key_and_limit(_req("/api/jobs"))
    assert key == "ip:1.2.3.4"
    assert limit == DEFAULT_LIMITS["general"]


def test_x_forwarded_for_first_hop_is_used() -> None:
    mw = _mw()
    req = _req("/api/jobs", {"x-forwarded-for": "9.9.9.9, 10.0.0.1"}, host="10.0.0.1")
    key, _ = mw._key_and_limit(req)
    assert key == "ip:9.9.9.9"  # real client, not the proxy hop


def test_raw_secret_never_appears_in_key() -> None:
    mw = _mw()
    secret = "tgd_live_supersecretvalue"
    key, _ = mw._key_and_limit(_req("/api/x", {"x-api-key": secret}))
    assert secret not in key  # hashed, not embedded


class _CountingLimiter:
    """In-memory stand-in for the Redis limiter with the same contract: every
    ``check`` records the request, and allows it while the bucket is within
    ``limit``. One window; the test never crosses a minute boundary."""

    def __init__(self) -> None:
        self.counts: dict[tuple[str, str], int] = {}

    async def check(self, key: str, operation: str, limit: int, window_seconds: int = 60) -> bool:
        self.counts[(key, operation)] = self.counts.get((key, operation), 0) + 1
        return self.counts[(key, operation)] <= limit

    async def get_remaining(self, key: str, operation: str, limit: int, window_seconds: int = 60) -> int:
        return max(0, limit - self.counts.get((key, operation), 0))


def _client_through_middleware():
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def ok(_request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[
        Route("/assets/{name}", ok),
        Route("/portal/login", ok, methods=["POST"]),
    ])
    app.add_middleware(TenantRateLimitMiddleware, limiter=_CountingLimiter())
    return TestClient(app)


def test_page_traffic_does_not_spend_the_login_allowance(monkeypatch) -> None:
    monkeypatch.delenv("GDX_E2E_BYPASS", raising=False)
    client = _client_through_middleware()
    # Several page loads' worth of anonymous asset requests from one IP —
    # more than the whole auth allowance.
    for i in range(DEFAULT_LIMITS["auth"] + 10):
        assert client.get(f"/assets/chunk{i}.js").status_code == 200
    assert client.post("/portal/login").status_code == 200


def test_login_attempts_are_still_limited(monkeypatch) -> None:
    monkeypatch.delenv("GDX_E2E_BYPASS", raising=False)
    client = _client_through_middleware()
    codes = [client.post("/portal/login").status_code for _ in range(DEFAULT_LIMITS["auth"] + 1)]
    assert codes[:-1] == [200] * DEFAULT_LIMITS["auth"]
    assert codes[-1] == 429
