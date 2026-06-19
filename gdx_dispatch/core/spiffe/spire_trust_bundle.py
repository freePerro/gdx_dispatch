"""SS-32 slice C — SPIRE trust-bundle fetch + cache.

The SPIRE server publishes a per-trust-domain bundle containing the
current X.509 authorities (PEM) and JWT authorities (JWKS). This module
provides a thread-safe cache that fetches the bundle from a configured
HTTP endpoint, serves it for a configurable TTL, refreshes on TTL
expiry, and — critically — continues to serve the last-known-good
bundle for a bounded stale window if a refresh fails.

Design notes
------------

* **Fail-available for a bounded time.** Rejecting every SVID because
  the SPIRE server blipped is worse than serving a bundle that's a few
  minutes stale. We serve the cached bundle up to ``MAX_STALE_SECONDS``
  (default 6h) past its TTL; after that we refuse and surface an error.
* **Warn on extended staleness.** After ``STALE_WARN_SECONDS`` (default
  10min past TTL) we log a warning once per refresh attempt so the
  operator sees that SPIRE is unreachable.
* **No silent failures.** Every refresh failure logs at warning level
  with the error; caller decides whether to treat stale-available as
  success or failure (it IS success for the validator — the bundle is
  still usable — but we emit an event separately).
* **Injectable fetcher.** Tests swap in a fake ``fetcher`` callable so
  we never hit real HTTP. Production wiring uses :func:`_default_fetcher`
  which uses ``httpx`` if available; otherwise ``urllib``.

Bundle format
-------------

The fetcher returns a dict matching SPIRE's bundle endpoint:
``{trust_domain: {"x509_authorities": [PEM, ...],
                  "jwt_authorities": [JWK, ...]}}``
which is the shape :mod:`svid_validator` consumes directly.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional

logger = logging.getLogger(__name__)


DEFAULT_TTL_SECONDS = 5 * 60        # 5 minutes
DEFAULT_MAX_STALE_SECONDS = 6 * 60 * 60  # 6 hours
DEFAULT_STALE_WARN_SECONDS = 10 * 60     # 10 minutes


Fetcher = Callable[[str], Dict[str, Any]]


class TrustBundleError(RuntimeError):
    """Raised when no usable bundle is available (not even stale)."""


@dataclass
class CachedBundle:
    """A snapshot of the trust bundle with timestamps."""

    bundle: Dict[str, Any]
    fetched_at: float
    ttl_seconds: int = DEFAULT_TTL_SECONDS

    def is_fresh(self, now: float) -> bool:
        return (now - self.fetched_at) < self.ttl_seconds

    def stale_seconds(self, now: float) -> float:
        return max(0.0, (now - self.fetched_at) - self.ttl_seconds)


def _default_fetcher(endpoint: str) -> Dict[str, Any]:
    """Fetch + JSON-parse the bundle from an HTTP endpoint.

    Prefer ``httpx`` if it's installed (it's in our deps); fall back to
    the stdlib ``urllib.request`` so this module stays import-safe in
    minimal test environments.
    """
    try:
        import httpx

        r = httpx.get(endpoint, timeout=10.0)
        r.raise_for_status()
        return r.json()
    except ImportError:  # pragma: no cover - httpx is in our deps
        import json
        import urllib.request

        with urllib.request.urlopen(endpoint, timeout=10.0) as resp:
            if resp.status >= 400:
                raise TrustBundleError(
                    f"SPIRE fetch HTTP {resp.status}"
                )
            return json.loads(resp.read().decode("utf-8"))


@dataclass
class TrustBundleCache:
    """Thread-safe SPIRE trust-bundle cache.

    Not a singleton — tests construct one per test; production wires one
    at app-start and holds it in module state where needed.
    """

    endpoint: str
    fetcher: Fetcher = field(default=_default_fetcher)
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    max_stale_seconds: int = DEFAULT_MAX_STALE_SECONDS
    stale_warn_seconds: int = DEFAULT_STALE_WARN_SECONDS
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _cache: Optional[CachedBundle] = field(default=None, repr=False)
    _last_error: Optional[str] = field(default=None, repr=False)
    _last_refresh_attempt: float = field(default=0.0, repr=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """Return a usable bundle (fresh, or stale within budget).

        On TTL expiry we try to refresh; if that fails and we still have
        a cached copy within :attr:`max_stale_seconds`, we serve it and
        warn. If we have no cached copy at all (first call after a
        failure), raise :class:`TrustBundleError`.
        """
        current = now if now is not None else time.time()
        with self._lock:
            if self._cache is not None and self._cache.is_fresh(current):
                return self._cache.bundle

            # Either no cache or stale.
            try:
                raw = self.fetcher(self.endpoint)
                self._last_refresh_attempt = current
                self._last_error = None
                self._cache = CachedBundle(
                    bundle=dict(raw),
                    fetched_at=current,
                    ttl_seconds=self.ttl_seconds,
                )
                return self._cache.bundle
            except Exception as exc:
                self._last_refresh_attempt = current
                self._last_error = str(exc)
                if self._cache is None:
                    logger.warning(
                        "spire trust bundle fetch failed and no cache "
                        "available: %s",
                        exc,
                    )
                    raise TrustBundleError(
                        f"unable to fetch trust bundle and no cache: {exc}"
                    ) from exc
                stale_age = self._cache.stale_seconds(current)
                if stale_age > self.max_stale_seconds:
                    logger.warning(
                        "spire trust bundle fetch failed; cache too "
                        "stale (%.0fs > max %ds): %s",
                        stale_age,
                        self.max_stale_seconds,
                        exc,
                    )
                    raise TrustBundleError(
                        f"trust bundle cache is {stale_age:.0f}s stale "
                        f"(max {self.max_stale_seconds}s); refusing"
                    ) from exc
                if stale_age > self.stale_warn_seconds:
                    logger.warning(
                        "spire trust bundle stale (%.0fs past TTL); "
                        "serving cached copy. refresh err: %s",
                        stale_age,
                        exc,
                    )
                else:
                    logger.warning(
                        "spire trust bundle refresh failed; serving "
                        "cached copy. refresh err: %s",
                        exc,
                    )
                return self._cache.bundle

    def force_refresh(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """Force a refresh regardless of TTL.

        Raises :class:`TrustBundleError` on failure (does NOT fall back
        to stale — this is an explicit operator action and they want to
        know it failed).
        """
        current = now if now is not None else time.time()
        with self._lock:
            try:
                raw = self.fetcher(self.endpoint)
            except Exception as exc:
                self._last_refresh_attempt = current
                self._last_error = str(exc)
                logger.warning(
                    "spire trust bundle force-refresh failed: %s", exc
                )
                raise TrustBundleError(
                    f"force refresh failed: {exc}"
                ) from exc
            self._last_refresh_attempt = current
            self._last_error = None
            self._cache = CachedBundle(
                bundle=dict(raw),
                fetched_at=current,
                ttl_seconds=self.ttl_seconds,
            )
            return self._cache.bundle

    def snapshot(self) -> Dict[str, Any]:
        """Return a diagnostic snapshot for the admin UI.

        Never raises. Includes freshness, last error, and a redacted
        (key-count-only) view of the cached bundle so tokens don't leak.
        """
        with self._lock:
            now = time.time()
            if self._cache is None:
                return {
                    "cached": False,
                    "endpoint": self.endpoint,
                    "last_error": self._last_error,
                    "last_refresh_attempt": self._last_refresh_attempt,
                }
            return {
                "cached": True,
                "endpoint": self.endpoint,
                "fetched_at": self._cache.fetched_at,
                "age_seconds": now - self._cache.fetched_at,
                "ttl_seconds": self.ttl_seconds,
                "fresh": self._cache.is_fresh(now),
                "stale_seconds": self._cache.stale_seconds(now),
                "last_error": self._last_error,
                "last_refresh_attempt": self._last_refresh_attempt,
                "trust_domains": sorted(self._cache.bundle.keys()),
                "authority_counts": {
                    td: {
                        "x509": len((e or {}).get("x509_authorities") or []),
                        "jwt": len((e or {}).get("jwt_authorities") or []),
                    }
                    for td, e in self._cache.bundle.items()
                },
            }
