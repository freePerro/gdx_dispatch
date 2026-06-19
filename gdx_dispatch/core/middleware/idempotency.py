"""Stripe-style Idempotency-Key middleware (SS-14 slice B).

# INTEGRATION TODO: register in gdx_dispatch/main.py (supervisor will handle).
#   app.add_middleware(IdempotencyMiddleware, redis_client=<redis>)
# Middleware assumes an upstream auth middleware has already populated
# ``request.state.principal`` with ``tenant_id`` + ``identity_id``
# attributes. When those are absent the middleware is a pure pass-through.

Contract (ss14-a pinned it):
    key = build_cache_key(tenant_id, identity_id, idempotency_key, path)

On a POST with header ``Idempotency-Key: <k>``:
    1. If no principal is attached to request.state, skip (no caching).
    2. Look up the cache key in Redis.
       - Hit: return the cached JSON body + status_code.
       - Miss: call downstream, capture the response body, and if the
         status is 2xx store it under TTL = IDEMPOTENCY_TTL_SECONDS.

Only JSON responses are cached. A non-JSON body (e.g. a streaming
download) is returned as-is and is NOT cached — we cannot safely replay
a body we cannot round-trip through ``json.loads``.

Redis is a fan-out cache, not source of truth. Any Redis error is
logged and the request is served without caching (fail-open).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gdx_dispatch.core.middleware.idempotency_keys import (
    IDEMPOTENCY_TTL_SECONDS,
    build_cache_key,
    is_cacheable_status,
)

log = logging.getLogger(__name__)
# Alias for the in-module body-cap log call (readability).
logger = log

# 0.9-s A4: response-body cap. Buffering unbounded response bytes to
# cache via Redis is a memory-DoS vector. Typical API responses are
# <100 KB; genuinely large responses (report exports, binary downloads)
# shouldn't be idempotency-cached anyway. 1 MB ceiling is generous.
IDEMPOTENCY_MAX_BODY_BYTES = 1 * 1024 * 1024


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Starlette middleware implementing Stripe-style Idempotency-Key replay."""

    def __init__(self, app: Any, redis_client: Any) -> None:
        super().__init__(app)
        self.redis = redis_client

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        # Only POST is idempotent-cacheable — GET is already idempotent,
        # and PUT/PATCH/DELETE semantics vary too much to cache safely.
        if request.method != "POST":
            return await call_next(request)

        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return await call_next(request)

        principal = getattr(request.state, "principal", None)
        if principal is None:
            return await call_next(request)

        tenant_id = getattr(principal, "tenant_id", None)
        identity_id = _coerce_identity_id(principal)
        if not tenant_id or not identity_id:
            # Can't key without both components; don't silently collide.
            return await call_next(request)

        cache_key = build_cache_key(
            str(tenant_id), str(identity_id), idempotency_key, request.url.path
        )

        cached = _safe_redis_get(self.redis, cache_key)
        if cached is not None:
            try:
                data = json.loads(cached)
                return JSONResponse(
                    content=data["body"],
                    status_code=int(data["status"]),
                )
            except (ValueError, KeyError, TypeError):
                log.warning(
                    "idempotency_cache_unparseable",
                    extra={"cache_key": cache_key},
                )
                # Fall through: treat as miss.

        response = await call_next(request)

        if not is_cacheable_status(response.status_code):
            return response

        # 0.9-s A4: cap the buffered response body. Upstream handlers
        # can legitimately produce large responses; buffering unbounded
        # bytes here is a memory-DoS vector. If the body exceeds the
        # cap we pass the chunks through without caching (streaming
        # response preserved, just not replayable — consistent with
        # the "only JSON is cached" rule below).
        body = b""
        oversize = False
        async for chunk in response.body_iterator:
            body += chunk
            if len(body) > IDEMPOTENCY_MAX_BODY_BYTES:
                oversize = True
                break

        if oversize:
            # Drain remaining chunks so the upstream iterator doesn't
            # leak, then hand the body back raw without caching.
            async for chunk in response.body_iterator:
                body += chunk
            logger.info(
                "idempotency: response body exceeded cache cap (%d bytes > %d); "
                "serving uncached",
                len(body), IDEMPOTENCY_MAX_BODY_BYTES,
            )
            return Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        # Only cache JSON-decodable bodies.
        try:
            body_json = json.loads(body) if body else None
        except ValueError:
            # Non-JSON response — return raw body, do not cache.
            return Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        _safe_redis_setex(
            self.redis,
            cache_key,
            IDEMPOTENCY_TTL_SECONDS,
            json.dumps({"status": response.status_code, "body": body_json}),
        )

        return JSONResponse(content=body_json, status_code=response.status_code)


def _coerce_identity_id(principal: Any) -> str | None:
    """Best-effort identity-id extractor.

    Principals in the codebase vary — SS-7 ``Principal`` uses ``subject``,
    while the SS-14 spec's pseudo-Principal uses ``identity_id``. Prefer
    ``identity_id`` when present; fall back to ``subject``.
    """
    ident = getattr(principal, "identity_id", None)
    if ident:
        return str(ident)
    subj = getattr(principal, "subject", None)
    if subj:
        return str(subj)
    return None


# Fail-open on any Redis transport failure. Module docstring §last:
# "Redis is a fan-out cache, not source of truth. Any Redis error is
# logged and the request is served without caching (fail-open)."
# Narrowed to (ConnectionError, TimeoutError, OSError, ValueError) —
# the shape the redis-py client raises for network / protocol errors.
# Upgraded from log.warning to log.error + structured extra so the
# supervisor can alert on a spike of cache-miss-because-redis-down.
# Counter (_REDIS_ERROR_COUNT) lets callers observe fail-open rate.
_REDIS_ERROR_COUNT: dict[str, int] = {"get": 0, "setex": 0}


def redis_error_counters() -> dict[str, int]:
    """Snapshot of Redis-failure counts for the idempotency middleware."""
    return dict(_REDIS_ERROR_COUNT)


def _is_redis_transport_error(exc: BaseException) -> bool:
    """Does this look like a redis-py transport / protocol failure?

    We import lazily and fall back to the standard-lib tuple so this
    module still imports cleanly without the redis package installed
    (tests sometimes mock the client).
    """
    if isinstance(exc, (ConnectionError, TimeoutError, OSError, ValueError)):
        return True
    try:
        from redis.exceptions import RedisError  # type: ignore
    except Exception:  # pragma: no cover — redis missing in trimmed envs
        return False
    return isinstance(exc, RedisError)


def _safe_redis_get(redis_client: Any, key: str) -> bytes | str | None:
    try:
        return redis_client.get(key)
    except Exception as exc:  # noqa: BLE001 — fail-open, see module-level comment
        if not _is_redis_transport_error(exc):
            # Not a transport failure — this is a programmer error
            # (e.g. AttributeError on a misconfigured client). Raise so
            # it's caught in test instead of silently becoming a cache
            # miss in production.
            raise
        _REDIS_ERROR_COUNT["get"] += 1
        log.error(
            "idempotency.redis_get_failed",
            extra={
                "op": "redis_get",
                "key": key,
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )
        return None


def _safe_redis_setex(redis_client: Any, key: str, ttl: int, value: str) -> None:
    try:
        redis_client.setex(key, ttl, value)
    except Exception as exc:  # noqa: BLE001 — fail-open, see module-level comment
        if not _is_redis_transport_error(exc):
            raise
        _REDIS_ERROR_COUNT["setex"] += 1
        log.error(
            "idempotency.redis_setex_failed",
            extra={
                "op": "redis_setex",
                "key": key,
                "ttl": ttl,
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )
