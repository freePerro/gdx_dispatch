"""Prometheus metrics middleware and /metrics endpoint for GDX.

Tracks HTTP request counts, latencies, active connections, and DB query timing.
The /metrics endpoint requires METRICS_TOKEN header for security.
"""
from __future__ import annotations

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
        tenant_id = request.headers.get("x-tenant-id", "unknown")
        # Normalize path to avoid high-cardinality (strip UUIDs)
        path = request.url.path
        parts = path.strip("/").split("/")
        normalized = "/" + "/".join(
            "{id}" if len(p) > 20 or _looks_like_uuid(p) else p
            for p in parts
        )
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


def _looks_like_uuid(s: str) -> bool:
    """Quick check if a string looks like a UUID (to normalize paths)."""
    return len(s) == 36 and s.count("-") == 4


router = APIRouter(tags=["metrics"])


@router.get("/metrics", include_in_schema=False)
async def metrics_endpoint(request: Request) -> PlainTextResponse:
    """Prometheus-compatible metrics scrape endpoint."""
    if not _AVAILABLE:
        raise HTTPException(status_code=503, detail="prometheus_client not installed")

    metrics_token = _get_metrics_token()
    if metrics_token:
        token = request.headers.get("x-metrics-token", "")
        if token != metrics_token:
            raise HTTPException(status_code=401, detail="Invalid metrics token")

    return PlainTextResponse(
        content=generate_latest(registry).decode("utf-8"),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
