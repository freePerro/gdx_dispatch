from __future__ import annotations

import functools
import inspect
import logging
import os
import time
from collections.abc import Callable
from enum import Enum
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, HTTPException
from redis.asyncio import Redis, from_url

logger = logging.getLogger(__name__)

KNOWN_SERVICES = ["db_provisioning", "stripe_api", "qb_api", "email_delivery"]


class CircuitOpenError(Exception):
    """Raised when the circuit is OPEN and a call is rejected."""


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@lru_cache(maxsize=1)
def get_redis_client() -> Redis:
    return from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
    )


class CircuitBreaker:
    """Redis-backed circuit breaker per external service.

    States:
      CLOSED    — normal operation; failures tracked.
      OPEN      — calls rejected immediately; waits recovery_timeout seconds.
      HALF_OPEN — after recovery_timeout elapses; one probe request allowed through.

    Redis keys (per service):
      cb:{name}:state      — CircuitState string value
      cb:{name}:failures   — integer failure count
      cb:{name}:opened_at  — unix timestamp when circuit was opened
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._redis = get_redis_client()

    # ── Redis key helpers ─────────────────────────────────────────────────────

    @property
    def _key_state(self) -> str:
        return f"cb:{self.name}:state"

    @property
    def _key_failures(self) -> str:
        return f"cb:{self.name}:failures"

    @property
    def _key_opened_at(self) -> str:
        return f"cb:{self.name}:opened_at"

    # ── State management ──────────────────────────────────────────────────────

    async def get_state(self) -> CircuitState:
        """Return current state; auto-transitions OPEN → HALF_OPEN after recovery_timeout."""
        raw_state = await self._redis.get(self._key_state)
        if raw_state is None:
            return CircuitState.CLOSED

        try:
            state = CircuitState(raw_state)
        except ValueError:
            logger.warning("circuit_breaker service=%s unknown state=%r, resetting", self.name, raw_state)
            return CircuitState.CLOSED

        if state == CircuitState.OPEN:
            opened_at_raw = await self._redis.get(self._key_opened_at)
            if opened_at_raw is not None:
                elapsed = time.time() - float(opened_at_raw)
                if elapsed > self.recovery_timeout:
                    await self._redis.set(self._key_state, CircuitState.HALF_OPEN.value)
                    logger.info(
                        "circuit_breaker service=%s OPEN→HALF_OPEN elapsed=%.1fs",
                        self.name,
                        elapsed,
                    )
                    return CircuitState.HALF_OPEN

        return state

    async def record_failure(self) -> None:
        """Increment failure count; open circuit if failure_threshold reached."""
        failures = await self._redis.incr(self._key_failures)
        logger.debug("circuit_breaker service=%s failures=%d", self.name, failures)

        if failures >= self.failure_threshold:
            current = await self._redis.get(self._key_state)
            if current != CircuitState.OPEN.value:
                await self._redis.set(self._key_state, CircuitState.OPEN.value)
                await self._redis.set(self._key_opened_at, str(time.time()))
                logger.warning(
                    "circuit_breaker service=%s OPENED after %d failures",
                    self.name,
                    failures,
                )

    async def record_success(self) -> None:
        """Reset circuit to CLOSED, clearing all Redis state."""
        prev_state = await self._redis.get(self._key_state)
        await self._redis.delete(self._key_state, self._key_failures, self._key_opened_at)
        await self._redis.set(self._key_state, CircuitState.CLOSED.value)
        if prev_state and prev_state != CircuitState.CLOSED.value:
            logger.info(
                "circuit_breaker service=%s %s→CLOSED (success)",
                self.name,
                prev_state,
            )

    async def reset(self) -> None:
        """Force-reset circuit to CLOSED (operator manual reset)."""
        await self.record_success()
        logger.info("circuit_breaker service=%s manually reset to CLOSED", self.name)

    async def get_status(self) -> dict[str, Any]:
        """Return a status snapshot dict for this circuit breaker."""
        state = await self.get_state()
        failures_raw = await self._redis.get(self._key_failures)
        opened_at_raw = await self._redis.get(self._key_opened_at)
        return {
            "name": self.name,
            "state": state.value,
            "failures": int(failures_raw) if failures_raw is not None else 0,
            "opened_at": float(opened_at_raw) if opened_at_raw is not None else None,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
        }

    async def call(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        """Execute *func* under circuit-breaker protection (legacy call-style API)."""
        state = await self.get_state()
        if state == CircuitState.OPEN:
            raise CircuitOpenError(f"Circuit '{self.name}' is OPEN — call rejected.")
        try:
            if inspect.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            await self.record_success()
            return result
        except CircuitOpenError:
            raise
        except Exception:
            await self.record_failure()
            raise


# ── Per-service helpers ───────────────────────────────────────────────────────

def get_circuit_breaker(service_name: str) -> CircuitBreaker:
    """Return a CircuitBreaker instance for the given service."""
    return CircuitBreaker(name=service_name)


async def get_all_circuit_breaker_statuses() -> list[dict[str, Any]]:
    """Return status snapshots for all known services."""
    statuses: list[dict[str, Any]] = []
    for svc in KNOWN_SERVICES:
        cb = CircuitBreaker(name=svc)
        try:
            status = await cb.get_status()
        except Exception as exc:  # noqa: BLE001
            logger.exception("circuit_breaker get_status failed service=%s: %s", svc, exc)
            status = {
                "name": svc,
                "state": "UNKNOWN",
                "failures": 0,
                "opened_at": None,
                "error": str(exc),
            }
        statuses.append(status)
    return statuses


# ── Decorator ─────────────────────────────────────────────────────────────────

def circuit_breaker(service_name: str) -> Callable:
    """Async decorator factory — wraps a FastAPI route with circuit breaker protection.

    Raises HTTP 503 when the circuit is OPEN.  Records failures on unhandled
    exceptions and resets to CLOSED on success.  HTTPExceptions are intentional
    responses and are *not* counted as failures.

    Usage::

        @router.post("/provision")
        @circuit_breaker("db_provisioning")
        async def provision_endpoint(...):
            ...
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            cb = CircuitBreaker(name=service_name)
            state = await cb.get_state()

            if state == CircuitState.OPEN:
                logger.warning(
                    "circuit_breaker BLOCKED request service=%s state=OPEN",
                    service_name,
                )
                raise HTTPException(
                    status_code=503,
                    detail=f"Service {service_name} circuit breaker is OPEN — try again later",
                )

            try:
                result = await func(*args, **kwargs)
                await cb.record_success()
                return result
            except HTTPException:
                # Intentional HTTP responses — do not count as circuit failures
                raise
            except Exception as exc:
                await cb.record_failure()
                logger.error(
                    "circuit_breaker recorded failure service=%s error=%s",
                    service_name,
                    exc,
                )
                raise

        return wrapper

    return decorator


# ── Pre-built circuit breakers (backward-compat aliases) ──────────────────────

qb_circuit = CircuitBreaker("qb_api", failure_threshold=5, recovery_timeout=120)
stripe_circuit = CircuitBreaker("stripe_api", failure_threshold=3, recovery_timeout=60)
email_circuit = CircuitBreaker("email_delivery", failure_threshold=10, recovery_timeout=300)


# ── Admin router ──────────────────────────────────────────────────────────────

router = APIRouter(prefix="/admin", tags=["circuit-breakers"])


@router.get("/circuit-breakers")
async def list_circuit_breakers() -> list[dict[str, Any]]:
    """GET /admin/circuit-breakers — view all circuit breaker states."""
    return await get_all_circuit_breaker_statuses()


@router.post("/circuit-breakers/{name}/reset")
async def reset_circuit_breaker(name: str) -> dict[str, Any]:
    """POST /admin/circuit-breakers/{name}/reset — manually reset a circuit to CLOSED."""
    if name not in KNOWN_SERVICES:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown circuit breaker: {name!r}. Known services: {KNOWN_SERVICES}",
        )
    cb = CircuitBreaker(name=name)
    await cb.reset()
    logger.info("circuit_breaker admin_reset service=%s", name)
    return {
        "name": name,
        "state": CircuitState.CLOSED.value,
        "message": "Circuit breaker reset to CLOSED",
    }
