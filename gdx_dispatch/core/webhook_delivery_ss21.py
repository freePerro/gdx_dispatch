"""
gdx_dispatch/core/webhook_delivery_ss21.py — SS-21 outbound webhook delivery.

Deliberately distinct filename from ``gdx_dispatch/core/webhook_delivery.py`` and
``gdx_dispatch/core/webhooks/delivery.py`` — those are the SS-6/7-era inbound
Zapier/CC webhook surface and are OUT OF SCOPE for SS-21 per the task
directive. This module is the new outbound surface that supports:

  * Dual-active HMAC-SHA256 signing (see gdx_dispatch.core.webhook_signing)
  * Retry backoff: 30s, 5m, 1h, 6h, 24h (5 attempts max)
  * Per-endpoint circuit breaker (open after N consecutive failures)
  * Structured failure records so the retry worker + operator see specific
    error_type and message (v3 patch P34 — never silent None-return on a
    non-HTTP exception)

INTEGRATION_TODO:
    - Persist delivery attempts + circuit-breaker state to the SS-21
      webhook_deliveries table once the migration is merged.
    - Hook up to a Celery beat schedule for retry dispatch.
    - Wire the HTTP client to a shared httpx.AsyncClient with connection pooling.
    - Emit audit events for circuit-open / circuit-close transitions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from gdx_dispatch.core.webhook_signing import (
    SIGNATURE_HEADER,
    SigningSecret,
    build_signature_header,
)

logger = logging.getLogger(__name__)

# Retry backoff ladder (seconds). Total wait ≈ 31h 5m 30s across 5 retries.
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (30, 300, 3600, 21600, 86400)

# Circuit breaker thresholds
CIRCUIT_OPEN_AFTER_FAILURES = 5  # open after this many consecutive 5xx/timeouts
CIRCUIT_HALF_OPEN_AFTER_SECONDS = 300  # 5m cool-down before retrying


# ---------------------------------------------------------------------------
# Result / delivery records
# ---------------------------------------------------------------------------


@dataclass
class DeliveryAttempt:
    subscription_id: str
    event_id: str
    url: str
    attempt_number: int
    status_code: Optional[int]
    error_type: Optional[str]
    error_msg: Optional[str]
    attempted_at: float
    succeeded: bool


@dataclass
class _CircuitState:
    consecutive_failures: int = 0
    opened_at: Optional[float] = None

    def is_open(self, now: float) -> bool:
        if self.opened_at is None:
            return False
        if now - self.opened_at >= CIRCUIT_HALF_OPEN_AFTER_SECONDS:
            return False  # half-open — allow a probe
        return True


class _CircuitBreakerRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_sub: dict[str, _CircuitState] = {}

    def get(self, sub_id: str) -> _CircuitState:
        with self._lock:
            return self._by_sub.setdefault(sub_id, _CircuitState())

    def record_success(self, sub_id: str) -> None:
        with self._lock:
            st = self._by_sub.setdefault(sub_id, _CircuitState())
            if st.opened_at is not None:
                logger.info("circuit CLOSE sub=%s after success", sub_id)
            st.consecutive_failures = 0
            st.opened_at = None

    def record_failure(self, sub_id: str, now: float) -> None:
        with self._lock:
            st = self._by_sub.setdefault(sub_id, _CircuitState())
            st.consecutive_failures += 1
            if (
                st.consecutive_failures >= CIRCUIT_OPEN_AFTER_FAILURES
                and st.opened_at is None
            ):
                st.opened_at = now
                logger.warning(
                    "circuit OPEN sub=%s after %d consecutive failures",
                    sub_id,
                    st.consecutive_failures,
                )

    def clear(self) -> None:
        with self._lock:
            self._by_sub.clear()


_circuit_registry = _CircuitBreakerRegistry()


def get_circuit_registry() -> _CircuitBreakerRegistry:
    return _circuit_registry


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


# Poster callable signature lets tests inject a stub without an HTTP client.
# Returns (status_code, error_type, error_msg). On success, error fields are None.
Poster = Callable[[str, bytes, dict[str, str]], "asyncio.Future[tuple[int, None, None]]"]


async def _default_http_post(
    url: str, body: bytes, headers: dict[str, str]
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Default poster using httpx.AsyncClient. Catches httpx.HTTPError ONLY
    (v3 patch P34). Non-HTTP exceptions propagate so bugs don't masquerade
    as 'delivery failed, retry forever'.
    """
    try:
        import httpx  # imported lazily so tests don't require the dep
    except ImportError:  # pragma: no cover
        return None, "ImportError", "httpx not installed"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                r = await client.post(url, content=body, headers=headers)
                return r.status_code, None, None
            except httpx.HTTPError as exc:
                return None, type(exc).__name__, str(exc)[:500]
    except Exception as exc:
        # Non-HTTP error — re-raise so the worker sees the real bug.
        logger.error("webhook delivery internal error: %s", exc)
        raise


async def deliver_webhook(
    *,
    subscription_id: str,
    event_id: str,
    url: str,
    event_payload: dict[str, Any],
    secrets: Sequence[SigningSecret],
    attempt_number: int = 1,
    poster: Optional[Callable[..., Any]] = None,
    now_fn: Callable[[], float] = time.time,
) -> DeliveryAttempt:
    """Deliver one webhook attempt. Caller owns retry scheduling.

    Pure per-attempt — does NOT sleep or reschedule. Returns a DeliveryAttempt
    recording the outcome. The caller (usually a Celery task using
    ``next_retry_delay``) decides whether to enqueue a retry.
    """
    now = now_fn()
    circuit = _circuit_registry.get(subscription_id)
    if circuit.is_open(now):
        return DeliveryAttempt(
            subscription_id=subscription_id,
            event_id=event_id,
            url=url,
            attempt_number=attempt_number,
            status_code=None,
            error_type="CircuitOpen",
            error_msg=f"circuit open since {circuit.opened_at}",
            attempted_at=now,
            succeeded=False,
        )

    # Serialize payload deterministically so the same event signs to the same
    # bytes even on retry (receivers may dedupe by signature).
    body = json.dumps(event_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    try:
        header, _ts = build_signature_header(secrets, body, timestamp=int(now))
    except Exception as exc:
        # Signing failed — this is a bug, not a delivery failure. Record and raise.
        logger.error("signing failed sub=%s event=%s: %s", subscription_id, event_id, exc)
        raise

    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: header,
        "X-GDX-Event-Id": event_id,
        "X-GDX-Attempt": str(attempt_number),
    }

    post = poster or _default_http_post
    try:
        status_code, error_type, error_msg = await post(url, body, headers)
    except Exception as exc:
        # v3 patch P34 — non-HTTP exceptions propagate (re-raise) BUT we still
        # want a DeliveryAttempt record so the worker has something to log.
        logger.error("non-HTTP delivery exc sub=%s: %s", subscription_id, exc)
        _circuit_registry.record_failure(subscription_id, now)
        # Re-raise after recording so the worker/alerting sees it.
        raise

    succeeded = status_code is not None and 200 <= status_code < 300
    if succeeded:
        _circuit_registry.record_success(subscription_id)
    else:
        _circuit_registry.record_failure(subscription_id, now)

    return DeliveryAttempt(
        subscription_id=subscription_id,
        event_id=event_id,
        url=url,
        attempt_number=attempt_number,
        status_code=status_code,
        error_type=error_type,
        error_msg=error_msg,
        attempted_at=now,
        succeeded=succeeded,
    )


def next_retry_delay(attempt_number: int) -> Optional[int]:
    """Return seconds to wait before attempt (attempt_number + 1), or None
    if we've exhausted the ladder."""
    # attempt_number is 1-indexed. After attempt 1 fails, we wait
    # RETRY_BACKOFF_SECONDS[0] before attempt 2, etc.
    idx = attempt_number - 1
    if idx < 0 or idx >= len(RETRY_BACKOFF_SECONDS):
        return None
    return RETRY_BACKOFF_SECONDS[idx]


__all__ = [
    "CIRCUIT_HALF_OPEN_AFTER_SECONDS",
    "CIRCUIT_OPEN_AFTER_FAILURES",
    "DeliveryAttempt",
    "RETRY_BACKOFF_SECONDS",
    "deliver_webhook",
    "get_circuit_registry",
    "next_retry_delay",
]
