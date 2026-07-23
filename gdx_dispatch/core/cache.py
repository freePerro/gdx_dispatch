"""Simple Redis cache helper for GDX hot-path endpoints.

Uses the existing async Redis client from the rate limiter module.
Gracefully degrades: if Redis is unavailable, the fetcher runs every time.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from gdx_dispatch.core.rate_limiter import get_redis_client

log = logging.getLogger(__name__)


async def cached(
    tenant_id: str,
    key: str,
    ttl_seconds: int,
    fetcher: Callable[[], Any],
) -> Any:
    """Try Redis cache first, fall back to *fetcher* on miss or error.

    Parameters
    ----------
    tenant_id:
        Scopes the cache key to a single tenant.
    key:
        Logical endpoint / query identifier (e.g. ``"customers:page=1:per=50"``).
    ttl_seconds:
        Time-to-live in seconds; Redis expires the key automatically.
    fetcher:
        Zero-arg callable that produces the canonical result (hits the DB).
        May be sync or async — if it returns an awaitable it will be awaited.
    """
    redis = get_redis_client()
    cache_key = f"cache:{tenant_id}:{key}"

    # --- read ---
    try:
        raw = await redis.get(cache_key)
        if raw is not None:
            log.debug("cache HIT  %s", cache_key)
            return json.loads(raw)
    except Exception:
        log.exception("cached_failed")
        log.debug("cache READ error for %s — skipping", cache_key, exc_info=True)

    # --- miss: call fetcher ---
    log.debug("cache MISS %s", cache_key)
    result = fetcher()
    # Support async fetchers transparently
    if hasattr(result, "__await__"):
        result = await result

    # --- write ---
    try:
        await redis.setex(cache_key, ttl_seconds, json.dumps(result, default=str))
    except Exception:
        log.exception("cached_failed")
        log.debug("cache WRITE error for %s — skipping", cache_key, exc_info=True)

    return result


async def invalidate(tenant_id: str, key: str) -> None:
    """Drop a cache entry written by ``cached()``.

    Call this from any handler that mutates the underlying data so the
    next GET refetches instead of serving the stale row. Safe to call
    when Redis is unavailable — the error is swallowed.
    """
    cache_key = f"cache:{tenant_id}:{key}"
    try:
        redis = get_redis_client()
        await redis.delete(cache_key)
        log.debug("cache INVALIDATE %s", cache_key)
    except Exception:
        log.debug("cache INVALIDATE error for %s — skipping", cache_key, exc_info=True)


async def invalidate_prefix(tenant_id: str, prefix: str) -> None:
    """Drop every cache entry whose key starts with *prefix* for the tenant.

    Needed where the cached key space fans out over query params (e.g. the
    customers list caches per ``q``/``page``/``per_page``) and a mutation
    must clear the whole family. The scan pattern stays anchored to one
    tenant + one logical endpoint, so the iteration is over a handful of
    keys, not the keyspace. Safe when Redis is down — errors swallowed,
    same contract as :func:`invalidate`.
    """
    pattern = f"cache:{tenant_id}:{prefix}*"
    try:
        redis = get_redis_client()
        async for key in redis.scan_iter(match=pattern, count=100):
            await redis.delete(key)
        log.debug("cache INVALIDATE-PREFIX %s", pattern)
    except Exception:
        log.debug("cache INVALIDATE-PREFIX error for %s — skipping", pattern, exc_info=True)


def invalidate_prefix_sync(tenant_id: str, prefix: str) -> None:
    """Sync fire-and-forget wrapper for :func:`invalidate_prefix` — for
    sync handlers (e.g. ``merge_customers``), mirroring invalidate_sync."""
    import asyncio

    coro = invalidate_prefix(tenant_id, prefix)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(coro)
        except Exception:
            log.debug("invalidate_prefix_sync run-loop error — skipping", exc_info=True)
        return
    try:
        loop.create_task(coro)
    except Exception:
        log.debug("invalidate_prefix_sync schedule error — skipping", exc_info=True)


def invalidate_sync(tenant_id: str, key: str) -> None:
    """Sync wrapper around :func:`invalidate` for sync handlers.

    Schedules the async delete on the running loop if one exists; otherwise
    runs it in a fresh loop. Either way, fire-and-forget.
    """
    import asyncio

    coro = invalidate(tenant_id, key)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — run synchronously in a fresh one.
        try:
            asyncio.run(coro)
        except Exception:
            log.debug("invalidate_sync run-loop error — skipping", exc_info=True)
        return
    # Running loop exists; schedule and don't wait.
    try:
        loop.create_task(coro)
    except Exception:
        log.debug("invalidate_sync schedule error — skipping", exc_info=True)
