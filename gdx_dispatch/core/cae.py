"""SS-10 Slice E — Continuous Access Evaluation (CAE) challenge primitive.

Minimal ``require_fresh_mfa`` helper. Read-only over ``Principal`` — this
slice does NOT widen the ``gdx_dispatch.core.principal.Principal`` dataclass. MFA
freshness fields are not yet modeled there, so lookups are done via
``getattr`` and a ``raw_claims`` fallback. Missing fields fail OPEN: the
helper returns silently so existing auth flows don't suddenly 401. Only
an *explicit* stale/false MFA signal raises.

The 401 response carries the CAE challenge header required by the
client-side refresh-and-retry flow::

    WWW-Authenticate: Bearer error="insufficient_claims", required="mfa"

There is no app wiring in this slice; routers / middleware will opt in
to this dependency in a later slice.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

logger = logging.getLogger(__name__)

_WWW_AUTHENTICATE_MFA = 'Bearer error="insufficient_claims", required="mfa"'


def _lookup(principal: Any, key: str) -> Any:
    """Fail-open read: attribute first, then ``raw_claims`` fallback."""
    sentinel = object()
    value = getattr(principal, key, sentinel)
    if value is not sentinel:
        return value
    claims = getattr(principal, "raw_claims", None)
    if isinstance(claims, dict):
        return claims.get(key)
    return None


def _coerce_verified_at(value: Any) -> datetime | None:
    """Normalize ``mfa_verified_at`` claim shapes to an aware ``datetime``.

    Accepts an aware ``datetime`` verbatim, or a POSIX ``int``/``float``
    seconds-since-epoch (common JWT shape). Anything else is ignored so
    the helper continues to fail open on malformed claims.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            # Pure parser: returning None on unparseable is the documented
            # contract (see function docstring). Caller decides how to
            # interpret None vs a valid datetime.
            logger.debug("cae._coerce_to_dt: unparseable timestamp %r: %s", value, exc)
            return None
    return None


def require_fresh_mfa(principal: Any, within_seconds: int = 300) -> None:
    """Require a recent MFA assertion on ``principal``; fail open on absence.

    Raises ``fastapi.HTTPException(401)`` with the CAE challenge header
    only when MFA is *explicitly* missing (``mfa_verified is False``) or
    the verification timestamp is older than ``within_seconds``. If the
    principal is ``None`` or carries no MFA-related fields at all, the
    helper returns silently — the broader ``Principal`` shape does not
    model MFA yet, and widening it is intentionally deferred.
    """
    if principal is None:
        return

    verified = _lookup(principal, "mfa_verified")
    verified_at_raw = _lookup(principal, "mfa_verified_at")

    if verified is False:
        raise HTTPException(
            status_code=401,
            detail="insufficient_claims",
            headers={"WWW-Authenticate": _WWW_AUTHENTICATE_MFA},
        )

    if verified_at_raw is not None:
        verified_at = _coerce_verified_at(verified_at_raw)
        if verified_at is not None:
            age = (datetime.now(timezone.utc) - verified_at).total_seconds()
            if age > within_seconds:
                raise HTTPException(
                    status_code=401,
                    detail="insufficient_claims",
                    headers={"WWW-Authenticate": _WWW_AUTHENTICATE_MFA},
                )
