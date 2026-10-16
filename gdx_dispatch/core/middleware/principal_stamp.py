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
* Applies the route dependency's gates (``finalize_login_jwt``) before
  stamping (GDXA-1302): a stamp lets SS-14 answer from cache ahead of the
  route's auth, so a revoked token or deactivated user must get no stamp.
* Rejects in exactly one case: a VERIFIED token that one of those gates
  refuses with 401 (denylisted jti, user missing/deleted/inactive, or the
  user lookup failing). The stamp answers that 401 itself rather than
  passing the request on uncached: the lookup cannot tell "user not
  eligible" from "database blipped", and on a blip the route's own lookup
  a moment later could succeed and run a money handler a second time —
  the duplicate the cache exists to stop. A gate's 403 (tenant mismatch,
  password-change, which cannot see the endpoint allowlist from here) and
  every unverifiable token still pass through unstamped, and the route's
  own auth answers as before.

When SS-9 lands and stamps a richer principal earlier in the stack, this
middleware detects the existing stamp and defers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StampedPrincipal:
    """Minimal principal: exactly what SS-14's cache key derivation needs."""

    tenant_id: str
    identity_id: str


@dataclass(frozen=True)
class _Refused:
    """A verified token that the route's 401 gates refuse."""

    detail: Any
    headers: dict[str, str] | None


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
            principal = _decode_to_principal(request, token)
            if isinstance(principal, _Refused):
                return JSONResponse(
                    {"detail": principal.detail}, status_code=401, headers=principal.headers,
                )
            if principal is not None:
                request.state.principal = principal
        return await call_next(request)


def _decode_to_principal(request: Request, token: str) -> StampedPrincipal | _Refused | None:
    """Verified-claims-or-nothing. A gate's 401 returns ``_Refused``; any
    other failure returns None (pass-through, no stamp).

    GDXA-1302: a stamp is what lets SS-14 answer from its cache BEFORE the
    route's ``get_current_user`` runs, so a token that dependency would
    refuse must not be stamped — otherwise a revoked token replays its
    cached 2xx. The decoded claims therefore go through the same
    ``finalize_login_jwt`` gate stack (denylist, user deleted/inactive,
    tenant match, password-change), not a copy of it that could drift.
    A 401 from those gates comes back as ``_Refused`` (see the module
    docstring for why it is answered here); a 403 means no stamp and the
    route answers. The password-change gate always refuses here because the
    endpoint is not resolved yet, so a flagged account simply gets no replay
    cache.
    """
    try:
        import jwt
        from starlette.exceptions import HTTPException

        from gdx_dispatch.routers.auth.core import ALG, VERIFY_KEY, finalize_login_jwt

        c = jwt.decode(token, VERIFY_KEY, algorithms=[ALG])
        if c.get("typ") not in (None, "access"):
            return None
        tenant_id = str(c.get("tenant_id", "") or c.get("gdx_tid", "") or "")
        identity_id = str(c.get("sub", "") or "")
        if not tenant_id or not identity_id:
            return None
        # finalize_login_jwt stashes request.state.user; this layer only
        # decides whether to stamp, so put back whatever was there.
        had_user = hasattr(request.state, "user")
        prior_user = getattr(request.state, "user", None)
        try:
            finalize_login_jwt(
                request,
                sub=identity_id,
                tenant_claim=tenant_id,
                role=str(c.get("role", "user")),
                actor_kind="human",
                jti=c.get("jti"),
            )
        except HTTPException as exc:
            if exc.status_code == 401:
                return _Refused(detail=exc.detail, headers=exc.headers)
            return None
        finally:
            if had_user:
                request.state.user = prior_user
            elif hasattr(request.state, "user"):
                del request.state.user
        return StampedPrincipal(tenant_id=tenant_id, identity_id=identity_id)
    except Exception:
        # Expired/malformed/wrong-key tokens are normal client state;
        # the route's own auth produces the 401. No cache, no noise.
        return None
