from __future__ import annotations

import hashlib
import os
import time
from functools import lru_cache
from typing import Any

from redis.asyncio import Redis, from_url
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Per-minute request ceilings. ``professional`` is the general per-caller limit
# (single-tenant: the one company is "active"); ``starter`` is retained for any
# caller without a paid context. ``auth`` is a deliberately stricter per-IP limit
# for the unauthenticated brute-force surface (login / signup).
DEFAULT_LIMITS: dict[str, int] = {
    "starter": 120,
    "professional": 600,
    "auth": 30,
}
DEFAULT_WINDOW: int = 60  # seconds


@lru_cache(maxsize=1)
def get_redis_client() -> Redis:
    return from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
    )


class RateLimiter:
    """
    Per-tenant sliding-window rate limiter backed by Redis sorted sets.

    Each call is recorded as a member of a sorted set where the score is the
    Unix timestamp (float).  Old entries are pruned on every check so the set
    always reflects only the current window.
    """

    @staticmethod
    def _key(tenant_id: str, operation: str) -> str:
        return f"ratelimit:{tenant_id}:{operation}"

    async def check(
        self,
        tenant_id: str,
        operation: str,
        limit: int,
        window_seconds: int = DEFAULT_WINDOW,
    ) -> bool:
        """
        Return True if the request is within the rate limit, False otherwise.
        Always records the current request.
        """
        redis = get_redis_client()
        key = self._key(tenant_id, operation)
        now = time.time()
        window_start = now - window_seconds

        pipe = redis.pipeline()
        # Remove entries outside the sliding window
        pipe.zremrangebyscore(key, "-inf", window_start)
        # Add the current timestamp (use unique member to allow same-second calls)
        pipe.zadd(key, {f"{now:.6f}-{id(object())}": now})
        # Count current entries
        pipe.zcard(key)
        # Reset TTL
        pipe.expire(key, window_seconds)
        results = await pipe.execute()

        current_count: int = results[2]
        return current_count <= limit

    async def get_remaining(
        self,
        tenant_id: str,
        operation: str,
        limit: int,
        window_seconds: int = DEFAULT_WINDOW,
    ) -> int:
        """Return the number of remaining allowed requests in the current window."""
        redis = get_redis_client()
        key = self._key(tenant_id, operation)
        now = time.time()
        window_start = now - window_seconds

        await redis.zremrangebyscore(key, "-inf", window_start)
        count = await redis.zcard(key)
        return max(0, limit - count)

    async def reset(self, tenant_id: str, operation: str) -> None:
        """Clear all rate-limit state for a tenant + operation pair."""
        redis = get_redis_client()
        await redis.delete(self._key(tenant_id, operation))


# Module-level singleton
rate_limiter = RateLimiter()


class TenantRateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate-limit middleware, keyed **per caller** (not per tenant).

    Single-tenant note: the old implementation keyed the bucket on the request's
    tenant id. With the single-tenant collapse that id is a constant, which would
    collapse every user, dashboard poll and login into ONE global bucket — a
    self-inflicted instance-wide 429 (including login) on the first busy minute.
    So keying is now per *caller*, chosen by what is reliably available in the
    middleware layer (before route auth runs):

      * unauthenticated auth/signup paths → per source IP, stricter ``auth``
        limit (OWASP: throttle the login/registration brute-force surface);
      * requests with ``X-API-Key``        → per API key (hashed);
      * requests with a Bearer token       → per session token (hashed);
      * everything else                    → per source IP.

    The class name is retained for import stability; it no longer reads
    ``request.state.tenant`` and so does not depend on TenantMiddleware.
    """

    _BYPASS_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc", "/metrics"})

    # Unauthenticated brute-force surface — keyed per IP with the stricter limit.
    # Matched by prefix so query strings and sub-paths are covered.
    _AUTH_PREFIXES = ("/auth/login", "/auth/platform-login", "/signup")

    def __init__(self, app: Any, limiter: RateLimiter | None = None) -> None:
        super().__init__(app)
        self._limiter = limiter or rate_limiter

    @staticmethod
    def _client_ip(request: Request) -> str:
        """Best-effort source IP. Trusts the FIRST hop of X-Forwarded-For (set
        by our own reverse proxy); falls back to the socket peer. Never throws."""
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _key_and_limit(self, request: Request) -> tuple[str, int]:
        """Resolve the rate-limit bucket key + per-minute limit for this request.

        Keyed per caller so one heavy user can't 429 everyone else (the bug a
        constant company-keyed bucket would cause in single-tenant)."""
        path = request.url.path
        # Auth/signup: no authenticated principal yet, and the abuse vector is
        # per-source — key by IP with the stricter limit.
        if any(path.startswith(p) for p in self._AUTH_PREFIXES):
            return f"ip:{self._client_ip(request)}", DEFAULT_LIMITS["auth"]
        # Authenticated / general traffic: prefer the most specific stable caller
        # identity. Hash the credential so raw secrets never land in a Redis key.
        api_key = request.headers.get("x-api-key")
        if api_key:
            return "key:" + hashlib.sha256(api_key.encode()).hexdigest()[:16], DEFAULT_LIMITS["professional"]
        authz = request.headers.get("authorization", "")
        if authz[:7].lower() == "bearer ":
            return "sess:" + hashlib.sha256(authz.encode()).hexdigest()[:16], DEFAULT_LIMITS["professional"]
        return f"ip:{self._client_ip(request)}", DEFAULT_LIMITS["professional"]

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if request.url.path in self._BYPASS_PATHS:
            return await call_next(request)

        # Bypass rate limiting for E2E test traffic (only when explicitly enabled)
        if os.getenv("GDX_E2E_BYPASS") == "1" and request.headers.get("x-e2e-test") == "true":
            return await call_next(request)

        # Per-caller key + limit (see _key_and_limit). Rate-limit by default for
        # every non-bypassed path (OWASP fail-safe-defaults) — no tenant context
        # needed, no per-request tier lookup.
        rate_key, limit = self._key_and_limit(request)
        operation = "http"

        try:
            allowed = await self._limiter.check(rate_key, operation, limit, DEFAULT_WINDOW)
            remaining = await self._limiter.get_remaining(rate_key, operation, limit, DEFAULT_WINDOW)
        except Exception:
            # Redis unavailable — pass through (same pattern as idempotency middleware)
            return await call_next(request)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please slow down."},
                headers={
                    "Retry-After": str(DEFAULT_WINDOW),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
