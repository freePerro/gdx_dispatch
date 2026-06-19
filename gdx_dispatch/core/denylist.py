"""SS-7 Slice C + Slice J — bounded denylist for JWT ``jti`` revocation.

Scope (bounded)
---------------
A Python, in-memory revocation list keyed by JWT ``jti`` with time-based
expiry. This module is the **pre-check surface** that sits *in front of*
the SS-7 policy evaluator: if a principal's token ``jti`` is on this
list (and has not yet reached its revocation expiry), the request is
rejected before :func:`gdx_dispatch.core.policy.evaluate` is ever called.

The denylist is deliberately NOT inside :mod:`gdx_dispatch.core.policy`:

* The policy evaluator answers "does this principal have permission
  for this action on this resource?" — a question about authorization
  that should be referentially transparent (same inputs → same output).
* The denylist answers "has this specific token been revoked since it
  was issued?" — a question about authentication lifecycle whose
  truthiness changes over time as admins revoke tokens.

Keeping them separate means the policy evaluator remains deterministic
and cheap to unit-test, while the denylist can be mutated at runtime
(admin revokes a token, token expires) without smuggling mutable state
into the authorization contract. Downstream wiring (SS-7 Slice D)
layers the two as:

    if denylist.contains(principal.jti):
        return 401  # token revoked
    return policy.evaluate(principal, action, resource)

Slice J — optional Redis backing for cross-worker visibility
------------------------------------------------------------
Slice H moved the denylist from a module global onto ``app.state`` so
the revoke writer and the auth reader share an instance *within one
FastAPI app*. That fixed the within-worker drift but left a different
drift visible in production: with ``--workers N > 1`` each worker has
its own app and therefore its own ``app.state.denylist``, so a revoke
landing on worker A is invisible to worker B until the admin hits B.

Slice J adds an *optional* Redis adapter seam:

* ``Denylist(redis_client=...)`` attaches a Redis client; omitting it
  restores the Slice C / Slice H local-only behavior byte-for-byte.
* ``add`` writes the local map first, then performs a best-effort
  ``SETEX`` against Redis. Redis errors are logged and swallowed.
* ``contains`` checks the local map first; on a local miss it performs
  a best-effort ``GET`` against Redis, hydrates the local map on a
  Redis hit, and returns ``False`` on a Redis error.
* The Redis adapter is **fail-open with local fallback** — a Redis
  outage cannot convert a valid login into a 401, and cannot convert
  a successful revoke into a 500. The revocation is already durable
  on the writing worker; Redis is fan-out, not source of truth.
* No hard runtime dependency on the ``redis`` package is introduced
  by this module — the client is a duck-typed object with ``setex``
  and ``get`` methods, so tests and callers that skip Redis never
  need to import it.

What this module does NOT do (deferred to later slices)
-------------------------------------------------------
* wire into FastAPI middleware or ``gdx_dispatch.core.auth_jwt`` (SS-7 Slice D),
* persist across Redis restarts (Redis is treated as a best-effort
  fan-out cache; a revocation Redis loses is still honored by the
  worker that wrote it until that worker's in-memory map expires),
* revoke tokens that have never been added (unknown ``jti`` is a miss),
* validate ``jti`` shape / length (callers pass the raw claim value).

Concurrency
-----------
The dict operations used here are atomic under CPython's GIL, but this
class does not add an explicit lock. Downstream wiring slices that put
the denylist behind async handlers should layer a :class:`threading.Lock`
or adopt a per-process singleton pattern.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

_REDIS_KEY_PREFIX = "ss7:denylist:"


def _utcnow() -> datetime:
    """Return a tz-aware UTC ``datetime``.

    Extracted so the ``now=None`` default has a single, documented
    source of truth; the bounded test suite prefers passing ``now``
    explicitly for determinism.
    """
    return datetime.now(timezone.utc)


@dataclass
class Denylist:
    """In-memory ``jti`` → ``expires_at`` map with optional Redis fan-out.

    Tests construct their own instance per test for isolation. Later
    wiring slices (SS-7 Slice D) decide whether to keep a module-level
    singleton or a per-process instance; this class makes no global
    assumption on their behalf.

    ``expires_at`` values are expected to be tz-aware UTC datetimes —
    typically the token's ``exp`` claim converted via
    :func:`datetime.fromtimestamp(..., tz=timezone.utc)`. Mixing naive
    and tz-aware datetimes will raise :class:`TypeError` at comparison
    time; that is intentional (it surfaces caller bugs loudly rather
    than silently producing wrong results).

    When ``redis_client`` is ``None`` the class behaves exactly like
    the Slice C / Slice H local-only denylist. When a client is
    supplied, ``add`` / ``contains`` additionally consult Redis with
    fail-open-on-error semantics (see module docstring).
    """

    _entries: dict[str, datetime] = field(default_factory=dict)
    redis_client: Any = None

    def add(self, jti: str, expires_at: datetime) -> None:
        """Revoke ``jti`` until ``expires_at``.

        Blank / empty ``jti`` is silently ignored — callers whose
        tokens lack a ``jti`` claim (malformed inputs, legacy tokens)
        must not crash the revocation path. Re-adding an existing
        ``jti`` overwrites the prior expiry (last write wins); this
        mirrors the typical "sliding-window revocation" pattern where
        a later ``exp`` supersedes an earlier one.

        Local write always happens first so the writing worker's own
        subsequent reads are consistent regardless of Redis state. The
        Redis ``SETEX`` is best-effort — any exception is logged and
        swallowed so a Redis outage cannot break the revoke endpoint.
        """
        if not jti:
            return
        self._entries[jti] = expires_at
        if self.redis_client is None:
            return
        # Best-effort fan-out to Redis. TTL is computed from the
        # caller-supplied ``expires_at`` so Redis naturally evicts the
        # entry at the same instant the local map treats it as expired.
        # A non-positive TTL means the entry is already expired — skip
        # the Redis write rather than pass an invalid TTL.
        try:
            ttl_seconds = int((expires_at - _utcnow()).total_seconds())
            if ttl_seconds <= 0:
                return
            self.redis_client.setex(
                _REDIS_KEY_PREFIX + jti,
                ttl_seconds,
                expires_at.isoformat(),
            )
        except Exception:
            # Fail-open: Redis is a fan-out cache, not source of truth.
            # The local write already succeeded.
            log.warning(
                "denylist_redis_write_failed",
                extra={"jti_len": len(jti)},
                exc_info=True,
            )

    def contains(self, jti: str, now: datetime | None = None) -> bool:
        """Return ``True`` iff ``jti`` is revoked AND not yet expired.

        Blank / empty ``jti`` is treated as a miss (as if never
        revoked). Expired entries are treated as misses and are
        opportunistically dropped so the map does not grow unboundedly
        in long-lived processes that never call :meth:`purge_expired`.

        Lookup order:

        1. Local map — if the jti is present we answer from memory, no
           Redis round-trip. An expired local entry is pruned and the
           method returns ``False``; we deliberately do NOT consult
           Redis in that case because a local observation of expiry is
           authoritative for this worker (Redis' TTL matches).
        2. Redis (if configured) — on a local miss, attempt a ``GET``.
           A hit rehydrates the local map so subsequent reads on this
           worker are fast-path. A Redis error returns ``False`` (fail-
           open miss) after logging — a Redis outage must not lock
           valid users out.
        """
        if not jti:
            return False
        current = now if now is not None else _utcnow()
        expires_at = self._entries.get(jti)
        if expires_at is not None:
            if expires_at <= current:
                self._entries.pop(jti, None)
                return False
            return True
        if self.redis_client is None:
            return False
        try:
            raw = self.redis_client.get(_REDIS_KEY_PREFIX + jti)
        except Exception:  # fail-open to prevent locking out valid users during Redis outages
            # Fail-open miss. The worker that wrote the revocation
            # still honors it locally; other workers will re-resolve
            # once Redis recovers.
            log.warning(
                "denylist_redis_read_failed",
                extra={"jti_len": len(jti)},
                exc_info=True,
            )
            return False
        if raw is None:
            return False
        try:
            redis_expires = datetime.fromisoformat(
                raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
            )
        except (TypeError, ValueError):  # fail-open to prevent locking out valid users during Redis outages
            log.warning("denylist_redis_value_unparseable")
            return False
        if redis_expires <= current:
            return False
        # Hydrate local map so the next contains() on this worker is a
        # local hit (and so purge_expired observes the entry too).
        self._entries[jti] = redis_expires
        return True

    def purge_expired(self, now: datetime | None = None) -> int:
        """Drop every local entry with ``expires_at <= now``; return count dropped.

        Complements the opportunistic cleanup in :meth:`contains`:
        :meth:`contains` only prunes the single ``jti`` it looked up,
        while this method sweeps the local map. Intended for a periodic
        janitor (e.g. a Celery beat task) once the denylist is wired
        into the request path.

        Redis entries expire on their own via the ``SETEX`` TTL set in
        :meth:`add`, so this method is local-only by design — a Redis
        fan-out sweep would be racy against live SETEX writes from
        other workers and is not needed for correctness.
        """
        current = now if now is not None else _utcnow()
        expired_jtis = [jti for jti, exp in self._entries.items() if exp <= current]
        for jti in expired_jtis:
            self._entries.pop(jti, None)
        return len(expired_jtis)
