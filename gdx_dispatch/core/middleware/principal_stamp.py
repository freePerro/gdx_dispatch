"""Stamp ``request.state.principal`` so the SS-14 idempotency cache engages.

Money-audit M36: the SS-14 ``IdempotencyMiddleware`` has been registered since
its slice landed, but it bails out unless an upstream layer populated
``request.state.principal`` — and nothing in production ever did (the only
assignment in the repo was in its own test file). The replay cache was a
permanent pass-through while the offline sync queue sent ``Idempotency-Key``
on every replay, and two money-path comments in ``routers/invoices.py``
described the middleware as protection it wasn't providing.

SS-9 (the dual-protocol ``get_current_user``) is the designed owner of this
stamp, but it does not exist. Until it does, this middleware closes the gap
minimally:

* Engages ONLY when the SS-14 cache would care: a POST carrying an
  ``Idempotency-Key`` header and a Bearer token. Everything else passes
  through untouched — no per-request decode tax on normal traffic.
* Decodes the token with the SAME key/algorithm configuration the auth
  dependency uses (lazy import of ``VERIFY_KEY``/``ALG`` — import-time would
  be circular). The signature IS verified: an unverifiable token stamps
  nothing, because a cache key must never be minted from attacker-writable
  claims.
* NEVER rejects. Auth enforcement stays where it lives — the route
  dependencies. A bad token here just means no replay cache for that
  request; the endpoint's own 401 happens downstream exactly as before.

When SS-9 lands and stamps a richer principal earlier in the stack, this
middleware detects the existing stamp and defers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StampedPrincipal:
    """Minimal principal: exactly what SS-14's cache key derivation needs."""

    tenant_id: str
    identity_id: str


class PrincipalStampMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any):  # type: ignore[override]
        if request.method != "POST":
            return await call_next(request)
        if not request.headers.get("Idempotency-Key"):
            return await call_next(request)
        if getattr(request.state, "principal", None) is not None:
            # A richer upstream stamp (SS-9, tests) wins — never overwrite.
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            principal = _decode_to_principal(token)
            if principal is not None:
                request.state.principal = principal
        return await call_next(request)


def _decode_to_principal(token: str) -> StampedPrincipal | None:
    """Verified-claims-or-nothing. Any failure returns None (pass-through)."""
    try:
        import jwt

        from gdx_dispatch.routers.auth.core import ALG, VERIFY_KEY

        c = jwt.decode(token, VERIFY_KEY, algorithms=[ALG])
        if c.get("typ") not in (None, "access"):
            return None
        tenant_id = str(c.get("tenant_id", "") or c.get("gdx_tid", "") or "")
        identity_id = str(c.get("sub", "") or "")
        if not tenant_id or not identity_id:
            return None
        return StampedPrincipal(tenant_id=tenant_id, identity_id=identity_id)
    except Exception:
        # Expired/malformed/wrong-key tokens are normal client state; the
        # route's own auth produces the 401. No cache, no noise.
        return None
