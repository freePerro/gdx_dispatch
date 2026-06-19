"""Refresh-token family revocation helper.

When a user's status changes in a way that should invalidate their open
sessions (deleted, deactivated, role demoted, password reset), we add
every JTI in their refresh family to the global `used_refresh_jtis` set.
The next refresh attempt for any of those tokens hits the replay-detected
path and dies. The currently-issued access token still works until its
~15-minute TTL expires, but no fresh access token can be minted.

Pre-fix (Sprint Auth & Identity Hardening Finding 2):
    `routers/users.py::delete_user` set `deleted_at = now()` and stopped.
    A grep across `gdx/` for `refresh_family` outside `routers/auth/core.py`
    returned zero hits — no other code path has ever revoked tokens. A
    deleted admin's tokens kept working until natural expiration; a
    demoted admin's role minted from JWT claims indefinitely (until
    Sprint Slice 1 closes the refresh-handler hole). This helper closes
    the second half of that loop: lifecycle events revoke now, the
    refresh handler verifies role on next mint.

Idempotent. Safe to call when there is no family marker (no-op). Safe to
call from request handlers — Redis ops are pipelined, ~1ms typical.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from redis import Redis

log = logging.getLogger(__name__)

# Mirror auth.py's TTL — both want the family marker to expire at the
# same wall-clock as the longest refresh token issued.
REFRESH_TTL = int(os.environ.get("REFRESH_TTL_SECONDS", str(60 * 60 * 24 * 7)))


def _redis_client() -> "Redis":
    """Lazy-import the redis client so unit tests that don't touch the
    revoke path don't have to spin up a fake redis."""
    from redis import from_url

    url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    return from_url(url, decode_responses=True)


def revoke_user_sessions(sub: str, *, reason: str = "user_lifecycle_event") -> int:
    """Revoke every refresh token currently in the user's family.

    Args:
        sub: the JWT `sub` value (tenant `users.id` for human users).
        reason: short label, surfaced in the log line and in audit logs
            written by callers. NOT persisted to redis itself.

    Returns:
        Count of JTIs marked as used (0 if no family marker existed).

    Never raises on Redis failure — logs and returns 0. The caller's
    primary action (deleting the user, demoting their role) must not
    fail just because Redis is briefly unavailable. Operational
    monitoring catches Redis outages elsewhere.
    """
    if not sub:
        return 0
    try:
        r = _redis_client()
    except Exception:  # noqa: BLE001 — defensive
        log.exception("auth_revoke_redis_unavailable sub=%s reason=%s", sub, reason)
        return 0

    try:
        family_key = f"refresh_family:{sub}"
        family_members = r.smembers(family_key) or set()
        if not family_members:
            return 0
        pipe = r.pipeline()
        for jti in family_members:
            pipe.sadd("used_refresh_jtis", jti)
        pipe.expire("used_refresh_jtis", REFRESH_TTL)
        pipe.delete(family_key)
        pipe.execute()
        log.info(
            "auth_sessions_revoked sub=%s count=%d reason=%s",
            sub,
            len(family_members),
            reason,
        )
        return len(family_members)
    except Exception:  # noqa: BLE001 — defensive
        log.exception("auth_revoke_failed sub=%s reason=%s", sub, reason)
        return 0
