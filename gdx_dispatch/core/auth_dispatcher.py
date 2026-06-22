"""Sprint 0.9 slice 0.9-d — composite ``get_current_principal`` dispatcher.

Accepts any of two auth flows and returns a unified
:class:`gdx_dispatch.core.unified_principal.Principal`:

* **session**  — session/JWT cookie-or-Bearer (SS-7)
* **spiffe**   — SPIFFE X.509 (mTLS-peer) or JWT-SVID (SS-32)

(The PAT, SCIM, and OAuth2 dev-portal flows were removed with the
single-tenant cleanup — the identity island and SS-21 authorization
server that backed them are gone.)

Dispatch order (highest priority first):

1. ``Authorization: Bearer <token>`` header. Sub-dispatch by token shape:

   * Three-segment ``eyJ...`` JWT shape → SPIFFE JWT-SVID if ``sub``
     starts with ``spiffe://``, else the SS-7 login-JWT flow.
   * Otherwise: opaque → 401 ``unknown_bearer_shape``.

2. Session cookie (``access_token``) → SS-7 session flow.

3. ``request.state.peer_spiffe_id`` (set by upstream mTLS layer) →
   SPIFFE X.509 flow.

4. Nothing authenticates → 401 ``missing_credentials``.

Stubs / future-slice markers
----------------------------
* **Session capabilities**: SS-7 ``Principal`` (``gdx_dispatch/core/principal.py``)
  carries no ``capabilities`` field. We fall back to an empty capability
  tuple for session principals; slice 0.9-e (router sweep) + Phase 3
  (role→caps map) finalize this.
* **SPIFFE tenant_id**: SPIFFE workloads are platform-wide by default.
  We synthesize a placeholder tenant UUID5 from the spiffe_id for the
  ``tenant_scope == "global"`` case; per-tenant workloads pass through a
  tenant id from ``request.state.tenant`` if set.
"""
from __future__ import annotations

import base64
import json
import logging
from uuid import UUID, uuid5, NAMESPACE_URL

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from gdx_dispatch.core.unified_principal import Principal
from gdx_dispatch.core.auth_capabilities import caps_for_role
from gdx_dispatch.core.tenant import single_tenant

log = logging.getLogger(__name__)

__all__ = [
    "get_current_principal",
    "require_role",
    "require_tenant_admin",
    "require_super_admin",
    "require_authenticated",
    "default_caps_for_role",
]


# ── Role → default capabilities (0.9-e scaffolding) ─────────────────────
#
# Minimal mapping so session-auth principals carry non-empty capability
# tuples out of the box. Phase 3 / SS-15 replaces this with a tenant-
# configurable map driven by ``platform_extensions.access_tokens``.


# DEPRECATED: Use gdx_dispatch.core.auth_capabilities.caps_for_role instead.
_DEFAULT_ROLE_CAPS: dict[str, tuple[tuple[str, str], ...]] = {
    # Platform super-admin — unrestricted wildcard.
    "super_admin": (("*", "*"),),
    # Tenant owner — full access inside the tenant plus the broad r/w
    # wildcards that session routes rely on.
    "owner": (
        ("*", "customers"),
        ("*", "jobs"),
        ("*", "invoices"),
        ("*", "leads"),
        ("read", "*"),
        ("write", "*"),
    ),
    # Tenant admin — admin surface, read-anything, no blanket write.
    "admin": (
        ("*", "customers"),
        ("*", "jobs"),
        ("*", "invoices"),
        ("*", "leads"),
        ("read", "*"),
    ),
    # Technician — job-scoped r/w, lead intake, customer read.
    "tech": (
        ("read", "jobs"),
        ("write", "jobs"),
        ("read", "customers"),
        ("read", "leads"),
        ("write", "leads"),
    ),
    # Read-only role.
    "viewer": (("read", "*"),),
    # SPIFFE workloads — caps come from workload_capability_map, not the role.
    "agent": (),
}


def default_caps_for_role(role: str) -> tuple[tuple[str, str], ...]:
    """Return the default capability tuple for a coarse role name.

    Unknown roles map to ``()`` (empty) — fail-closed. Phase 3 / SS-15
    will replace this call site with a tenant-configurable lookup.
    """
    return _DEFAULT_ROLE_CAPS.get(role, ())

# Module-level sentinel UUID namespaces for synthesizing stable ids
# where a real row id is not (yet) available. Distinct from
# SPIFFE_ID_NAMESPACE so collisions cannot span scopes.
_SESSION_IDENTITY_NAMESPACE = uuid5(NAMESPACE_URL, "gdx:session_identity_synth")

# Reserved tenant slug for platform-scoped (non-tenant) principals —
# e.g. SPIFFE workloads with ``tenant_scope == "global"``. Chosen with
# underscores so it CANNOT collide with a real ``tenants.slug`` value
# (slugs are `[a-z0-9-]+` per onboarding validation). Platform ORM
# filters comparing ``tenant_id == principal.tenant_id`` will miss
# every row — which is the correct fail-closed behavior for a platform
# principal reaching into tenant-scoped data.
_PLATFORM_TENANT_SLUG = "__platform__"


# ── Shape detection helpers (pure, cheap) ────────────────────────────────

def _looks_like_jwt(token: str) -> bool:
    """Shape check only — three base64url segments separated by dots.

    Does not validate signature, claims, or audience. Callers downstream
    do full verification.
    """
    if not token.startswith("eyJ"):
        return False
    return token.count(".") == 2


def _jwt_has_spiffe_sub(token: str) -> bool:
    """Return True iff the (unverified) JWT payload's ``sub`` claim starts
    with ``spiffe://``.

    Used only for dispatch routing — the signature is validated later in
    ``_dispatch_spiffe_jwt``. A malformed payload returns False (falls
    through to OAuth dispatch, which will reject it loudly).
    """
    try:
        _, payload_b64, _ = token.split(".")
        # JWT uses base64url without padding — reintroduce padding for b64decode.
        pad = "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + pad)
        payload = json.loads(payload_bytes)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    sub = payload.get("sub")
    return isinstance(sub, str) and sub.startswith("spiffe://")


# ── Capability translation helpers ────────────────────────────────────────


def _colon_cap_to_tuple(flat: str) -> tuple[str, str] | None:
    """Translate an SS-22 SCIM colon-flattened capability string
    ``"<action>:<resource>"`` into the unified 2-tuple.

    Returns None for malformed entries.
    """
    if not isinstance(flat, str) or flat.count(":") != 1:
        return None
    action, resource = flat.split(":", 1)
    if not action or not resource:
        return None
    return (action, resource)


# ── Per-flow dispatch shims ──────────────────────────────────────────────


def _get_db_or_none(request: Request) -> Session | None:
    """Extract a DB session from request state if the host app provided one.

    The dispatcher can't create a session on its own (that would duplicate
    engine wiring); host apps with a DB dep should set
    ``request.state.db`` before dispatch. Returns None if not available —
    PAT / OAuth dispatch will raise a 503-ish HTTPException in that case.
    """
    return getattr(request.state, "db", None)


async def _dispatch_spiffe_jwt(request: Request, token: str) -> Principal:
    """Validate a SPIFFE JWT-SVID and return a unified Principal.

    Trust bundle + expected audiences come from app.state (populated by
    :class:`gdx_dispatch.core.middleware.spiffe_auth_middleware.SPIFFEAuthMiddleware`
    wiring). If the host app has not wired SPIFFE, we 503 — presenting a
    JWT-SVID to an app that can't verify it is a config error, not a 401.
    """
    from gdx_dispatch.core.spiffe.svid_validator import JWTSVIDError, validate_jwt_svid
    from gdx_dispatch.core.spiffe.workload_capability_map import resolve_capabilities

    bundle_cache = getattr(request.app.state, "spiffe_trust_bundle", None)
    audiences = getattr(request.app.state, "spiffe_audiences", None)
    if bundle_cache is None or not audiences:
        raise HTTPException(
            status_code=503,
            detail={
                "error_type": "spiffe_not_wired",
                "detail": "SPIFFE JWT-SVID presented but host app has no trust bundle configured",
            },
        )

    try:
        # TrustBundleCache exposes a zero-arg ``.get()`` returning a dict;
        # tests/dev may pass a plain dict directly. Distinguish by checking
        # whether ``get`` is bound with a default-zero arity.
        if isinstance(bundle_cache, dict):
            bundle = bundle_cache
        elif hasattr(bundle_cache, "get") and callable(bundle_cache.get):
            bundle = bundle_cache.get()
        else:
            bundle = bundle_cache
        validated = validate_jwt_svid(
            token, trust_bundle=bundle, expected_audiences=audiences
        )
    except JWTSVIDError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error_type": "spiffe_jwt_invalid", "detail": str(exc)},
        ) from exc

    resolved = resolve_capabilities(validated.spiffe_id.uri)
    caps = _translate_spiffe_caps(resolved.capabilities)
    tenant_id = _spiffe_tenant_id(request, validated.spiffe_id.uri, resolved.tenant_scope)

    return Principal.from_spiffe(
        spiffe_id=validated.spiffe_id.uri,
        tenant_id=tenant_id,
        capabilities=caps,
    )


async def _dispatch_spiffe_mtls(request: Request) -> Principal:
    """Build a Principal for an mTLS-peer SPIFFE identity.

    ``request.state.peer_spiffe_id`` is set by an upstream layer (SPIRE
    agent sidecar, envoy SDS, etc.). We trust that assertion — the TLS
    handshake already verified the peer cert against the trust bundle.
    """
    from gdx_dispatch.core.spiffe.workload_capability_map import resolve_capabilities

    peer_id = getattr(request.state, "peer_spiffe_id", None)
    if not isinstance(peer_id, str) or not peer_id.startswith("spiffe://"):
        # Should never reach here if the caller gated on this, but fail loud.
        raise HTTPException(
            status_code=401,
            detail={
                "error_type": "spiffe_mtls_invalid",
                "detail": "request.state.peer_spiffe_id is not a valid SPIFFE ID",
            },
        )

    resolved = resolve_capabilities(peer_id)
    caps = _translate_spiffe_caps(resolved.capabilities)
    tenant_id = _spiffe_tenant_id(request, peer_id, resolved.tenant_scope)

    return Principal.from_spiffe(
        spiffe_id=peer_id,
        tenant_id=tenant_id,
        capabilities=caps,
    )


def _translate_spiffe_caps(
    caps: tuple[str, ...],
) -> list[tuple[str, str]]:
    """Translate SS-32 workload-capability-map strings into unified tuples.

    SS-32 emits colon-flattened strings like ``"invoke:mcp.tool"`` or bare
    action strings. We prefer colon-split; bare strings map to
    ``(action, "*")``.
    """
    out: list[tuple[str, str]] = []
    for c in caps:
        if not isinstance(c, str) or not c:
            continue
        if ":" in c:
            t = _colon_cap_to_tuple(c)
            if t is not None:
                out.append(t)
        else:
            out.append((c, "*"))
    return out


def _spiffe_tenant_id(
    request: Request, spiffe_id: str, tenant_scope: str
) -> str:
    """Resolve a tenant slug for a SPIFFE principal.

    * If ``request.state.tenant["slug"]`` is set (the tenant
      middleware resolved a concrete tenant from host/header), use it.
    * If ``tenant_scope == "global"``, return the reserved platform
      slug (:data:`_PLATFORM_TENANT_SLUG`).
    * Otherwise, the workload is tenant-scoped but no concrete tenant
      was resolved — return a namespaced ``spiffe-unresolved:...``
      slug so platform ORM filters miss loudly rather than fall
      through to a synthesized hash that could accidentally collide.
    """
    # Single-tenant: every request resolves to the one pinned company, so its
    # slug (single_tenant()["slug"]) is authoritative. Pre-collapse this read
    # the host-resolved tenant from request.state.tenant; host-based resolution
    # no longer exists, and single_tenant() always yields a non-empty slug, so
    # the first branch always returns here. The "global"/unresolved branches are
    # kept (unreachable today) so the SPIFFE scope contract survives intact if
    # multi-tenancy is ever reintroduced.
    slug = single_tenant().get("slug")
    if isinstance(slug, str) and slug:
        return slug
    if tenant_scope == "global":
        return _PLATFORM_TENANT_SLUG
    return f"spiffe-unresolved:{spiffe_id}"


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

    from gdx_dispatch.core.auth import validate_principal
    from gdx_dispatch.core.auth_jwt import JWTValidationError
    from gdx_dispatch.routers.auth.core import (
        ALG,
        VERIFY_KEY,
        _get_app_denylist,
        finalize_login_jwt,
    )

    sub: str | None = None
    tenant_claim: str | None = None
    role: str = "user"
    jti: str | None = None
    actor_kind: str = "human"
    imp_actor_id: str | None = None
    imp_purpose: str | None = None

    # Primary path — SS-7 RS256 validator. Pass the app-state denylist
    # (Slice H) so revoke-writes via /auth/admin/revoke take effect for
    # routers on the dispatcher too.
    public_keys: dict[str, bytes | str] = {}
    if ALG == "RS256" and VERIFY_KEY:
        public_keys = {"gdx-spa": VERIFY_KEY, "gdx-thirdparty": VERIFY_KEY}

    primary_failed = False
    try:
        validated = validate_principal(
            token,
            public_keys_by_provider=public_keys,
            denylist=_get_app_denylist(request),
        )
        # typ guard on the primary path (matches the legacy decode below).
        if validated.raw_claims.get("typ") not in (None, "access"):
            raise HTTPException(
                status_code=401,
                detail={"error_type": "invalid_login_jwt", "detail": "JWT is not an access token"},
            )
        sub = validated.subject
        tenant_claim = validated.tenant_id
        role = str(validated.raw_claims.get("role") or "user")
        jti = validated.jti
        actor_kind = (
            getattr(validated.actor_kind, "value", None) or str(validated.actor_kind or "human")
        )
        imp_actor_id = validated.raw_claims.get("imp_actor_id")
        imp_purpose = validated.raw_claims.get("imp_purpose")
    except JWTValidationError:
        primary_failed = True

    if primary_failed:
        # Legacy decode path — locally-signed RS256 / HS256 tokens minted
        # by `_issue()` lack the Authentik iss/aud shape and fall here.
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
        imp_actor_id = payload.get("imp_actor_id")
        imp_purpose = payload.get("imp_purpose")

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
    #     /auth/admin/revoke takes effect on EVERY login JWT shape, not
    #     just Authentik-shape tokens that flow through validate_principal.
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
        imp_actor_id=imp_actor_id,
        imp_purpose=imp_purpose,
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
    authenticates; ``HTTPException(503)`` when a credential needs backing
    infra the host app has not wired (e.g. DB for PAT, trust bundle for
    SPIFFE JWT).
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
            if _jwt_has_spiffe_sub(token):
                return await _dispatch_spiffe_jwt(request, token)
            return await _dispatch_login_jwt(request, token)

        # Opaque bearer token — no recognized shape. The OAuth2 dev-portal
        # authorization server (SS-21) was removed with the single-tenant
        # cleanup; the only bearer tokens we accept now are login JWTs
        # (handled above) and SPIFFE JWT-SVIDs.
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

    # 3. mTLS-derived SPIFFE peer identity
    if getattr(request.state, "peer_spiffe_id", None):
        return await _dispatch_spiffe_mtls(request)

    # 4. No credential material present
    raise HTTPException(
        status_code=401,
        detail={
            "error_type": "missing_credentials",
            "detail": "No session cookie, Authorization header, or SPIFFE mTLS peer identity present",
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
require_super_admin = require_role("super_admin")


# ``require_authenticated`` is simply an alias for ``get_current_principal``
# — any principal that resolves is authenticated. Separate name is for
# router readability: ``Depends(require_authenticated)`` vs
# ``Depends(get_current_principal)`` when the route just wants auth
# without a role gate.
require_authenticated = get_current_principal
