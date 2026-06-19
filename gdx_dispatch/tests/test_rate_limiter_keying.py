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
    for path in ("/auth/login", "/auth/platform-login", "/signup", "/signup?ref=x"):
        key, limit = mw._key_and_limit(_req(path))
        assert key == "ip:1.2.3.4", path
        assert limit == DEFAULT_LIMITS["auth"], path  # stricter than general


def test_api_key_keyed_per_key_not_per_company() -> None:
    mw = _mw()
    key, limit = mw._key_and_limit(_req("/api/customers", {"x-api-key": "tgd_live_abc"}))
    assert key.startswith("key:")
    assert limit == DEFAULT_LIMITS["professional"]
    # Two different keys land in different buckets (no shared global bucket).
    k1, _ = mw._key_and_limit(_req("/api/x", {"x-api-key": "aaa"}))
    k2, _ = mw._key_and_limit(_req("/api/x", {"x-api-key": "bbb"}))
    assert k1 != k2


def test_bearer_keyed_per_session() -> None:
    mw = _mw()
    key, limit = mw._key_and_limit(_req("/api/jobs", {"authorization": "Bearer xyz"}))
    assert key.startswith("sess:")
    assert limit == DEFAULT_LIMITS["professional"]


def test_anonymous_falls_back_to_per_ip() -> None:
    mw = _mw()
    key, limit = mw._key_and_limit(_req("/api/jobs"))
    assert key == "ip:1.2.3.4"
    assert limit == DEFAULT_LIMITS["professional"]


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
