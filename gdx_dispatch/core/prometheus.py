"""Prometheus metrics middleware and /metrics endpoint for GDX.

Tracks HTTP request counts, latencies, active connections, and DB query timing.
The /metrics endpoint requires the METRICS_TOKEN header for security. With
METRICS_TOKEN unset the endpoint refuses to serve rather than serving the
whole registry to anyone (it failed open until 2026-09-04).

Cardinality is the other half of keeping this endpoint safe. The registry lives
in process memory, is never evicted, and only resets on a redeploy — so a label
value that varies with untrusted input is an unbounded memory leak driven by
whoever wants to drive it. Both label sources that did that are closed: the
`tenant_id` label reads the server-verified tenant rather than a client header,
and `endpoint` is the MATCHED ROUTE rather than the requested path (#597, see
`_endpoint_label`).
"""
from __future__ import annotations

import hmac
import logging
import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _AVAILABLE = True
except ImportError:  # pragma: no cover
    logging.getLogger(__name__).exception("<module> caught exception")
    _AVAILABLE = False

def _get_metrics_token() -> str:
    return os.getenv("METRICS_TOKEN", "")

_SKIP_PATHS = frozenset({"/metrics", "/health", "/favicon.ico"})

if _AVAILABLE:
    registry = CollectorRegistry(auto_describe=False)

    http_requests_total = Counter(
        "http_requests_total",
        "Total HTTP requests",
        ["method", "endpoint", "status", "tenant_id"],
        registry=registry,
    )

    http_request_duration_seconds = Histogram(
        "http_request_duration_seconds",
        "HTTP request duration in seconds",
        ["method", "endpoint"],
        registry=registry,
    )

    active_requests = Gauge(
        "active_requests",
        "Active HTTP requests",
        registry=registry,
    )

    db_query_duration_seconds = Histogram(
        "db_query_duration_seconds",
        "Database query duration in seconds",
        ["operation"],
        registry=registry,
    )
else:
    registry = None  # type: ignore[assignment]


@contextmanager
def track_db_query(operation: str) -> Generator[None, None, None]:
    """Context manager to time DB operations for Prometheus."""
    if not _AVAILABLE:
        yield
        return
    start = time.monotonic()
    try:
        yield
    finally:
        db_query_duration_seconds.labels(operation=operation).observe(
            time.monotonic() - start
        )


async def prometheus_middleware(request: Request, call_next: Any) -> Any:
    """ASGI middleware that tracks request metrics."""
    if not _AVAILABLE or request.url.path in _SKIP_PATHS:
        return await call_next(request)

    active_requests.inc()
    start = time.monotonic()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        active_requests.dec()
        duration = time.monotonic() - start
        # Server-verified tenant only. The x-tenant-id header this once read
        # was multi-tenant residue: any client could stamp any label value.
        tenant_id = str((getattr(request.state, "tenant", None) or {}).get("id", "-"))
        normalized = _endpoint_label(request)
        http_requests_total.labels(
            method=request.method,
            endpoint=normalized,
            status=str(status_code),
            tenant_id=tenant_id,
        ).inc()
        http_request_duration_seconds.labels(
            method=request.method,
            endpoint=normalized,
        ).observe(duration)


#: Label for a request that matched nothing at all. ONE value, not one per
#: URL — that is the whole point. In THIS app very little reaches it, because
#: the SPA catch-all (`/{full_path:path}`) matches unknown paths first; it is
#: the floor for a deployment with no catch-all, and for ASGI scopes that never
#: reach the router.
UNMATCHED_ENDPOINT = "<unmatched>"

#: Bucket for a Starlette `Mount` (StaticFiles and friends). One value per
#: mount, not one per asset filename.
MOUNT_ENDPOINT_FMT = "{root}/*"


def _endpoint_label(request: Request) -> str:
    """A BOUNDED label for this request: what it matched, never what it asked for.

    The registry is in-memory, unbounded, and reset only by a redeploy, so every
    distinct `endpoint` value is a permanent new series. This used to be the
    requested path, collapsed only when a segment was longer than 20 characters
    or looked like a uuid::

        "{id}" if len(p) > 20 or _looks_like_uuid(p) else p

    so every junk URL minted its own series, driven by unauthenticated traffic.
    Measured on 24h of real production request paths (2026-09-10): **1,075
    distinct paths produced 770 distinct label values**, 257 of them scanner
    probes like `/.git/config` and `/1.php`. Nothing evicts them; ~4.6 KB per
    counter+histogram pair.

    Four sources, in order, each drawn from a FIXED set:

    1. ``scope["route"].path`` — the route template. Prefixed with ``root_path``
       so a mounted sub-app's ``/ping`` cannot merge with a top-level ``/ping``.
    2. ``root_path`` alone — a ``Mount`` matched but set no route. One bucket
       per mount (``/assets/*``), not one per asset.
    3. the handler's name — a plain ``starlette.routing.Route`` matched.
    4. ``UNMATCHED_ENDPOINT``.

    Steps 2 and 3 exist because **only FastAPI's ``APIRoute.matches`` sets
    ``scope["route"]``** — plain ``Route`` and ``Mount`` do not (verified
    against starlette 1.6.0 / fastapi 0.141.1). Reading route alone would have
    labelled every static asset and the ``/mcp`` route ``<unmatched>``, pooling
    real 200s with scanner 404s and making the sentinel the busiest series in
    the registry — bounded, but useless.
    """
    scope = request.scope
    root = scope.get("root_path") or ""

    template = getattr(scope.get("route"), "path", None)
    if isinstance(template, str) and template:
        return f"{root}{template}" if root else template

    if root:
        return MOUNT_ENDPOINT_FMT.format(root=root)

    endpoint = scope.get("endpoint")
    if endpoint is not None:
        name = getattr(endpoint, "__qualname__", None) or type(endpoint).__name__
        return f"<route:{name}>"

    return UNMATCHED_ENDPOINT


router = APIRouter(tags=["metrics"])


@router.get("/metrics", include_in_schema=False)
async def metrics_endpoint(request: Request) -> PlainTextResponse:
    """Prometheus-compatible metrics scrape endpoint."""
    if not _AVAILABLE:
        raise HTTPException(status_code=503, detail="prometheus_client not installed")

    metrics_token = _get_metrics_token()
    if not metrics_token:
        # Fail CLOSED. The previous `if metrics_token:` made an unset token
        # mean "no auth required", so prod served the entire registry —
        # 4.1 MB of route, tenant and timing data — to anyone who asked.
        # An unconfigured scrape secret is a misconfiguration, not consent.
        raise HTTPException(
            status_code=503,
            detail="metrics endpoint is not configured (set METRICS_TOKEN)",
        )
    # Compare as BYTES. compare_digest raises TypeError on str operands
    # containing non-ASCII, and Starlette latin-1-decodes header values, so a
    # header of b"caf\xe9" would otherwise crash the endpoint with an
    # unauthenticated 500 instead of returning 401.
    token = request.headers.get("x-metrics-token", "")
    if not hmac.compare_digest(token.encode("utf-8"), metrics_token.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid metrics token")

    return PlainTextResponse(
        content=generate_latest(registry).decode("utf-8"),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
