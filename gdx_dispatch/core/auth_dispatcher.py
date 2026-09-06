"""Sprint 0.9 slice 0.9-d — composite ``get_current_principal`` dispatcher.

Accepts the one auth flow this app has and returns a unified
:class:`gdx_dispatch.core.unified_principal.Principal`:

* **session**  — the login JWT, as a Bearer header or the ``access_token``
  cookie (SS-7)

(The PAT, SCIM and OAuth2 dev-portal flows went with the single-tenant
cleanup; the SPIFFE JWT-SVID / mTLS flows went 2026-09-06 — no SPIRE
deployment ever existed and ``SPIFFE_ENABLE`` was set nowhere.)

Dispatch order:

1. ``Authorization: Bearer <token>`` header — a three-segment ``eyJ...``
   JWT is the login-JWT flow; any other shape → 401 ``unknown_bearer_shape``.
2. Session cookie (``access_token``) → the same flow.
3. Nothing authenticates → 401 ``missing_credentials``.

Stubs / future-slice markers
----------------------------
* **Session capabilities**: session principals carry no ``capabilities``
  field, so we fall back to an empty capability tuple. (The SS-7
  ``Principal`` this once referenced lived in ``core/principal.py``, which
  went with the Authentik validator.)
"""
from __future__ import annotations

import logging
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import Depends, HTTPException, Request

from gdx_dispatch.core.auth_capabilities import caps_for_role
from gdx_dispatch.core.unified_principal import Principal

log = logging.getLogger(__name__)

__all__ = [
    "get_current_principal",
    "require_role",
    "require_tenant_admin",
    "require_authenticated",
]


# Module-level sentinel UUID namespace for synthesizing stable ids where a
# real row id is not (yet) available.
_SESSION_IDENTITY_NAMESPACE = uuid5(NAMESPACE_URL, "gdx:session_identity_synth")


# ── Shape detection helpers (pure, cheap) ────────────────────────────────

def _looks_like_jwt(token: str) -> bool:
    """Shape check only — three base64url segments separated by dots.

    Does not validate signature, claims, or audience. Callers downstream
    do full verification.
    """
    if not token.startswith("eyJ"):
        return False
    return token.count(".") == 2


async def _dispatch_login_jwt(request: Request, token: str) -> Principal:
    """Decode a verified login JWT minted by /auth/login and build a
    session-style Principal — running the SAME post-decode gates that
    :func:`gdx_dispatch.routers.auth.core.get_current_user` runs (Slice 2 DB-verify,
    Slice 6 tenant-match, Slice H denylist). Both deps share the
    ``finalize_login_jwt`` helper to prevent the gates from drifting.

    D-S118-dispatcher-jwt-gap (Doug 2026-05-10): pre-fix, /api/pats and
    /api/capabilities/available (and 7 other admin routes) returned 401
    because JWT-shaped Bearer tokens routed only to ``_dispatch_oauth``
    (which only knows the OAuth in-memory store; login JWTs aren't there).
    Bug authored 2026-04-20 in Sprint 0.9-d's composite-dispatcher build;
    surfaced 2026-05-10 by /settings/api-keys SettingsApiKeys.vue.

    Round-1 fix tried to mirror only the decode and skipped the three
    after-decode gates — auditor caught it as P0 auth bypass. Round-2
    factors the gates into ``finalize_login_jwt`` and calls it here.
    """
    # Lazy imports — auth_dispatcher is imported very early; auth.core
    # transitively imports a large dependency graph.
    import jwt as _jwt_lib

    from gdx_dispatch.routers.auth.core import (
        ALG,
        VERIFY_KEY,
        finalize_login_jwt,
    )

    # Single decode path. There used to be a "primary" branch here running
    # the Authentik access-token validator, with this as the fallback.
    # Nothing ever reached it: `_issue()` mints tokens carrying only
    # sub/tenant_id/role/jti/typ/exp, so the validator rejected every real
    # token for a missing `iss` claim. Authentik is gone.
    try:
        payload = _jwt_lib.decode(token, VERIFY_KEY, algorithms=[ALG])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=401,
            detail={
                "error_type": "invalid_login_jwt",
                "detail": "Bearer token is not a valid login JWT",
            },
        ) from exc
    if payload.get("typ") not in (None, "access"):
        raise HTTPException(
            status_code=401,
            detail={
                "error_type": "invalid_login_jwt",
                "detail": "JWT is not an access token",
            },
        )
    sub = str(payload.get("sub") or "")
    tenant_claim = payload.get("gdx_tid") or payload.get("tenant_id")
    role = str(payload.get("role") or "user")
    jti = payload.get("jti")
    actor_kind = "human"

    if not sub:
        raise HTTPException(
            status_code=401,
            detail={
                "error_type": "invalid_login_jwt",
                "detail": "Login JWT lacks 'sub' claim",
            },
        )

    # ─── Slice H + 2 + 6 gates via the shared finalizer ────────────────
    # finalize_login_jwt:
    #   - Slice H: consults the request.app.state denylist on the jti so
    #     /auth/admin/revoke takes effect on every login JWT.
    #     (D-S119-legacy-denylist-gap, surfaced 2026-05-10 by prod walk.)
    #   - DB-verifies the user (denies missing/deleted/inactive)
    #   - Overlays role from users.role (closes the demoted-admin gap)
    #   - Stashes user_dict on request.state.user
    #   - Calls _enforce_tenant_match (raises 403 on mismatch)
    # Returns user_dict with the verified role + tenant_id.
    user_dict = finalize_login_jwt(
        request,
        sub=sub,
        tenant_claim=str(tenant_claim or ""),
        role=role,
        actor_kind=actor_kind,
        jti=jti,
    )

    # Build the Principal from the *verified* user_dict (verified role,
    # not the JWT-claim role).
    try:
        identity_id = UUID(user_dict["user_id"])
    except (ValueError, TypeError):
        identity_id = uuid5(_SESSION_IDENTITY_NAMESPACE, f"login_jwt:{user_dict['user_id']}")

    verified_role = str(user_dict.get("role") or "user")
    caps = caps_for_role(verified_role)

    return Principal.from_session(
        identity_id=identity_id,
        tenant_id=str(user_dict.get("tenant_id") or ""),
        role=verified_role,
        capabilities=caps,
        session_id=str(jti or ""),
    )


async def _dispatch_session(request: Request) -> Principal:
    """Build a Principal from the `access_token` session cookie.

    Only the JWT-shape `access_token` cookie (set by /auth/login) triggers
    session dispatch. The cookie is routed through ``_dispatch_login_jwt``
    so it receives the same gate stack as bearer-presented login JWTs —
    signature verification, typ guard, Slice 2 DB-verify, Slice 6 tenant-
    match, Slice H denylist.

    History: pre-D-S119, this function also accepted `session` and `sid`
    cookie names and synthesized a Principal from arbitrary cookie values.
    Research (2026-05-10) confirmed `gdx_dispatch/app.py` never registers
    Starlette's SessionMiddleware, so nothing in our system sets `session`
    or `sid` cookies — the branch was dead code that weakened the auth
    surface (any XSS that set `session=foo` got a usable role='user'
    Principal with zero caps). Branch deleted in D-S119-opaque-cookie-
    deprecate.
    """
    session_token = request.cookies.get("access_token")
    if not session_token:
        # Should never reach here; caller gates on this.
        raise HTTPException(
            status_code=401,
            detail={
                "error_type": "missing_credentials",
                "detail": "No access_token cookie present",
            },
        )

    # Cookie must be a JWT. The access_token cookie is set by /auth/login
    # and /auth/refresh which always mint JWTs; a non-JWT here means a
    # forged cookie or a stale client — reject loudly.
    if not _looks_like_jwt(session_token):
        raise HTTPException(
            status_code=401,
            detail={
                "error_type": "invalid_session_cookie",
                "detail": "access_token cookie is not a valid JWT",
            },
        )

    return await _dispatch_login_jwt(request, session_token)


# ── The composite dependency ─────────────────────────────────────────────


async def get_current_principal(request: Request) -> Principal:
    """FastAPI composite dependency: resolve the current request's principal.

    Dispatches by auth material shape. See module docstring for the full
    priority list. Raises ``HTTPException(401)`` when no credential shape
    authenticates.
    """
    # 1. Authorization header
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if not token:
            raise HTTPException(
                status_code=401,
                detail={
                    "error_type": "empty_bearer",
                    "detail": "Authorization header has Bearer scheme but empty token",
                },
            )

        if _looks_like_jwt(token):
            return await _dispatch_login_jwt(request, token)

        # Opaque bearer token — no recognized shape. The OAuth2 dev-portal
        # authorization server (SS-21) was removed with the single-tenant
        # cleanup; the only bearer tokens we accept are login JWTs.
        raise HTTPException(
            status_code=401,
            detail={
                "error_type": "unknown_bearer_shape",
                "detail": "Authorization uses Bearer scheme but token shape is not recognized",
            },
        )

    # 2. Session cookie — only the JWT-shape access_token cookie set by
    # /auth/login. `session` and `sid` cookies are no longer accepted
    # (D-S119-opaque-cookie-deprecate, 2026-05-10): nothing in our system
    # sets them, and accepting them weakened the auth surface to any XSS
    # that managed to set a session cookie value.
    if request.cookies.get("access_token"):
        return await _dispatch_session(request)

    # 3. No credential material present
    raise HTTPException(
        status_code=401,
        detail={
            "error_type": "missing_credentials",
            "detail": "No session cookie or Authorization header present",
        },
    )


# ── Shared role-gate helpers (0.9-e) ─────────────────────────────────────
#
# Factories returning FastAPI deps. ``require_role("owner", "admin")``
# returns a dep that raises 403 unless the resolved principal's
# ``principal_role`` is in the allowed set. Thin wrapper on top of
# ``get_current_principal`` so routers never read roles off raw
# ``request.state``.


def require_role(*allowed_roles: str):
    """FastAPI dep factory — gate a route on one or more coarse roles.

    Usage::

        @router.get("/admin/thing")
        def handler(
            principal: Principal = Depends(require_role("owner", "admin")),
        ):
            ...

    Raises ``HTTPException(403)`` with an ``insufficient_role`` error shape
    when ``principal.principal_role`` is not in ``allowed_roles``. The
    allowed roles are sorted in the error payload so responses are stable
    regardless of the order the caller passed them.
    """
    allowed = frozenset(allowed_roles)

    async def _check(
        principal: Principal = Depends(get_current_principal),
    ) -> Principal:
        if principal.principal_role not in allowed:
            raise HTTPException(
                status_code=403,
                detail={
                    "error_type": "insufficient_role",
                    "detail": (
                        f"role '{principal.principal_role}' not in "
                        f"{sorted(allowed)}"
                    ),
                    "required_roles": sorted(allowed),
                },
            )
        return principal

    return _check


# Canonical role gates built on ``require_role``. These are deps
# themselves — use them directly in ``Depends(...)``.
require_tenant_admin = require_role("owner", "admin")


# ``require_authenticated`` is simply an alias for ``get_current_principal``
# — any principal that resolves is authenticated. Separate name is for
# router readability: ``Depends(require_authenticated)`` vs
# ``Depends(get_current_principal)`` when the route just wants auth
# without a role gate.
require_authenticated = get_current_principal
