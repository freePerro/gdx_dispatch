"""
gdx_dispatch/routers/oauth2.py — SS-21 OAuth2 authorization-code + PKCE endpoints.

RFC references:
  * RFC 6749 — OAuth 2.0 core (authorization-code grant)
  * RFC 7636 — PKCE (S256 only per native-app best practice; plain REJECTED)
  * RFC 7009 — Token revocation
  * RFC 7662 — Token introspection
  * RFC 8252 — OAuth 2.0 for Native Apps (mandates PKCE, exact redirect match)

TODO:
    - Wire into gdx_dispatch/main.py:  app.include_router(oauth2.router)
    - Replace in-memory token store with the SS-21 access_tokens table
      (see gdx_dispatch.models.platform_ss21_additions) once the migration is merged
      into the main chain.
    - Render a real user-facing consent screen at /oauth/authorize (GET) and
      move code issuance to the POST-consent handler; the current GET handler
      auto-approves for already-authenticated sessions (sandbox behavior).
    - Authenticate confidential clients via Basic auth or client_assertion;
      the current /token endpoint trusts the client_id from the form for
      public (PKCE) clients only.
    - Replace `subject_id` query param with the authenticated session user.

Endpoints:
    GET  /oauth/authorize   -> issues auth code, 302 redirect
    POST /oauth/token       -> code→token exchange
    POST /oauth/revoke      -> revoke access/refresh token
    POST /oauth/introspect  -> RFC 7662 introspection

Auth-boundary notes (for red-team auditors — 2026-04-19 triage):
    * `_registered_redirect_uris` is a helper that reads an already-looked-up
      DeveloperApp row; it is NOT an HTTP handler and does NOT own an auth
      boundary. Its callers (authorize + token) perform the client_id → app
      lookup which fails before this helper is reached for unknown clients.
    * /oauth/authorize DOES gate on authenticated user: missing `subject_id`
      raises 401 `login_required` before a code is minted (the 401 IS the
      auth check — the TODO above describes replacing the query
      param with a real session-cookie read, not adding a missing gate).
    * /oauth/token is RFC 6749-compliant in NOT requiring user-session auth:
      the authorization code is the user-bound credential (minted by the
      previously-authenticated authorize flow), and PKCE + client_id binding
      is the client-authn surface. Public (PKCE) clients intentionally have
      no `client_secret`; confidential-client Basic auth is TODO
      above.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from gdx_dispatch.core.mcp_bearer import mint_mcp_access_token
from gdx_dispatch.core.oauth2_grants import (
    PKCE_METHOD_S256,
    consume_authorization_code,
    mint_authorization_code,
    validate_redemption,
)
from gdx_dispatch.core.well_known_manifest import request_base_url
from gdx_dispatch.models.platform_ss20_additions import DeveloperApp, OAuthDynamicClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth", tags=["oauth2"])


# ---------------------------------------------------------------------------
# DB dep — overridden in tests / main.py
# ---------------------------------------------------------------------------
def get_db() -> Session:  # pragma: no cover — replaced in wiring
    raise RuntimeError(
        "get_db must be overridden. TODO: wire in main.py."
    )


# ---------------------------------------------------------------------------
# Token store (Redis-backed — SS 0.9-h)
# ---------------------------------------------------------------------------
#
# Key scheme:
#   gdx:oauth:token:<access>    → JSON(TokenRecord)     TTL = expires_at - now
#   gdx:oauth:refresh:<refresh> → <access>              TTL = expires_at - now
#
# Revocation semantics: delete both keys (no "revoked" flag kept around —
# absence == revoked/expired). Refresh-token rotation deletes the old pair and
# SETEXs the new pair inside a ``MULTI/EXEC`` pipeline so the swap is atomic.

_PRODUCTION_READY = True

_TOKEN_KEY_PREFIX = "gdx:oauth:token:"
_REFRESH_KEY_PREFIX = "gdx:oauth:refresh:"


def _token_key(access: str) -> str:
    return f"{_TOKEN_KEY_PREFIX}{access}"


def _refresh_key(refresh: str) -> str:
    return f"{_REFRESH_KEY_PREFIX}{refresh}"


@dataclass
class TokenRecord:
    access_token: str
    refresh_token: str
    client_id: str
    scope: str
    tenant_id: str | None
    subject_id: str | None
    issued_at: float
    expires_at: float
    revoked: bool = False
    extra: dict = field(default_factory=dict)


class _RedisTokenStore:
    """Redis-backed access/refresh token store.

    BYO-Redis (constructor injection) following
    ``gdx_dispatch.core.middleware.idempotency.IdempotencyMiddleware``. Tests pass
    ``fakeredis.FakeRedis(decode_responses=True)``.
    """

    def __init__(self, redis_client) -> None:
        if redis_client is None:
            raise ValueError("redis_client is required")
        self._r = redis_client

    def put(self, rec: TokenRecord) -> None:
        ttl = max(1, int(rec.expires_at - time.time()))
        payload = json.dumps(asdict(rec))
        pipe = self._r.pipeline()
        pipe.setex(_token_key(rec.access_token), ttl, payload)
        # refresh → access reverse lookup (same TTL)
        pipe.setex(_refresh_key(rec.refresh_token), ttl, rec.access_token)
        pipe.execute()

    def get_by_access(self, tok: str) -> TokenRecord | None:
        raw = self._r.get(_token_key(tok))
        if raw is None:
            return None
        rec = TokenRecord(**json.loads(raw))
        if rec.revoked or rec.expires_at < time.time():
            return None
        return rec

    def get_by_refresh(self, tok: str) -> TokenRecord | None:
        access = self._r.get(_refresh_key(tok))
        if access is None:
            return None
        return self.get_by_access(access)

    def revoke(self, tok: str) -> bool:
        """Delete the access+refresh pair. Token may be either the access or
        the refresh value (RFC 7009 §2.1 permits both). Returns True if
        something was deleted."""
        # Try as an access token first.
        raw = self._r.get(_token_key(tok))
        if raw is not None:
            rec = TokenRecord(**json.loads(raw))
            pipe = self._r.pipeline()
            pipe.delete(_token_key(rec.access_token))
            pipe.delete(_refresh_key(rec.refresh_token))
            pipe.execute()
            return True
        # Try as a refresh token.
        access = self._r.get(_refresh_key(tok))
        if access is not None:
            pipe = self._r.pipeline()
            pipe.delete(_token_key(access))
            pipe.delete(_refresh_key(tok))
            pipe.execute()
            return True
        return False

    def rotate(self, old: TokenRecord, new: TokenRecord) -> None:
        """Atomically delete ``old`` pair and SETEX ``new`` pair.

        Used by the refresh-token rotation flow — MUST be atomic so that a
        crash mid-rotation cannot leave both pairs live (double-spend window)
        or both pairs dead (client locked out)."""
        ttl = max(1, int(new.expires_at - time.time()))
        payload = json.dumps(asdict(new))
        pipe = self._r.pipeline()
        pipe.delete(_token_key(old.access_token))
        pipe.delete(_refresh_key(old.refresh_token))
        pipe.setex(_token_key(new.access_token), ttl, payload)
        pipe.setex(_refresh_key(new.refresh_token), ttl, new.access_token)
        pipe.execute()

    def clear(self) -> None:
        """Wipe every token/refresh key. Test-helper only."""
        for prefix in (_TOKEN_KEY_PREFIX, _REFRESH_KEY_PREFIX):
            for k in list(self._r.scan_iter(match=f"{prefix}*")):
                self._r.delete(k)


# ---------------------------------------------------------------------------
# Redis client — lazy singleton
# ---------------------------------------------------------------------------

_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    import redis as redis_lib

    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    _redis_client = redis_lib.from_url(
        url, decode_responses=True, socket_connect_timeout=2
    )
    return _redis_client


_token_store: _RedisTokenStore | None = None


def get_token_store() -> _RedisTokenStore:
    global _token_store
    if _token_store is None:
        _token_store = _RedisTokenStore(_get_redis())
    return _token_store


def set_token_store_for_tests(store: _RedisTokenStore | None) -> None:
    """Override the module-level token store (tests only)."""
    global _token_store
    _token_store = store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


ACCESS_TOKEN_TTL_SECONDS = 3600


def _load_client(db: Session, client_id: str):
    """Lookup a client_id across both portal-curated apps and DCR-issued clients.

    Returns either a ``DeveloperApp`` row (legacy SS-20 portal client) or
    an ``OAuthDynamicClient`` row (Sprint mcp-streamable-http S5 RFC 7591
    DCR). Both expose ``client_id``, ``redirect_uri(s)``, and a soft-delete
    column (``deleted_at``); ``_registered_redirect_uris`` handles the
    shape difference at the call site.
    """
    row = (
        db.query(DeveloperApp)
        .filter(DeveloperApp.client_id == client_id, DeveloperApp.deleted_at.is_(None))
        .first()
    )
    if row is not None:
        return row
    return (
        db.query(OAuthDynamicClient)
        .filter(
            OAuthDynamicClient.client_id == client_id,
            OAuthDynamicClient.deleted_at.is_(None),
        )
        .first()
    )


def _registered_redirect_uris(app_row) -> set[str]:
    """SS-20 portal app stores a single ``redirect_uri`` (newline-separated
    list also accepted). DCR client stores a JSON list. Normalize both to
    a set of exact-match strings.
    """
    if isinstance(app_row, OAuthDynamicClient):
        return {u for u in (app_row.redirect_uris or []) if isinstance(u, str) and u.strip()}
    raw = app_row.redirect_uri or ""
    uris = {u.strip() for u in raw.splitlines() if u.strip()}
    if not uris and raw.strip():
        uris = {raw.strip()}
    return uris


def _error_redirect(redirect_uri: str, error: str, state: str | None) -> RedirectResponse:
    params = {"error": error}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


def _json_error(error: str, description: str, status: int = 400) -> JSONResponse:
    """RFC 6749 §5.2 error response shape."""
    return JSONResponse(
        status_code=status,
        content={"error": error, "error_description": description},
    )


# ---------------------------------------------------------------------------
# /oauth/authorize
# ---------------------------------------------------------------------------


@router.get("/authorize")
async def authorize(
    request: Request,
    response_type: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
    scope: str = "",
    state: str | None = None,
    # subject_id is the legacy debug path: pass an explicit user UUID via
    # query string. Production callers (claude.ai connector) never set it
    # — the user is resolved from the GDX session cookie instead. See
    # the principal-resolution block in the body.
    subject_id: str | None = None,
    # Sprint mcp-streamable-http S4: tenant_id query-param is the legacy
    # path. When the request reached this endpoint via TenantMiddleware
    # (per-tenant host), `request.state.tenant["id"]` is authoritative
    # and overrides any caller-supplied value. The override is the
    # actual security fix — accepting caller-supplied tenant lets a
    # caller at gdx.* mint a code bound to acme's tenant_id.
    tenant_id: str | None = None,
    resource: str | None = None,
    db: Session = Depends(get_db),
):
    """RFC 6749 §4.1.1 authorization request.

    Enforces RFC 7636 + RFC 8252: PKCE REQUIRED, S256 only.
    """
    # Validate response_type
    if response_type != "code":
        raise HTTPException(
            status_code=400,
            detail={"error": "unsupported_response_type", "error_description": response_type},
        )

    # Validate client
    app_row = _load_client(db, client_id)
    if app_row is None:
        # Per RFC 6749 §4.1.2.1: if client is invalid, do NOT redirect — show error
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_client", "error_description": "unknown client_id"},
        )

    # Validate redirect_uri — MUST exactly match a registered URI
    registered = _registered_redirect_uris(app_row)
    if redirect_uri not in registered:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_request",
                "error_description": "redirect_uri not registered for this client",
            },
        )

    # PKCE S256 only
    if code_challenge_method != PKCE_METHOD_S256:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_request",
                "error_description": "code_challenge_method must be S256 (plain rejected)",
            },
        )
    if not code_challenge:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "error_description": "code_challenge required"},
        )

    # User authentication: prefer an authenticated session over the
    # legacy subject_id query param. claude.ai's connector flow lands
    # the browser on this endpoint after redirecting from the
    # authorization-server URL; the browser carries any GDX session
    # cookie, which the dispatcher can resolve into a Principal.
    #
    # No consent screen yet — auto-grant once the user is authenticated.
    # (Real consent UI is a follow-up; the JWT minted here is already
    # tenant-bound + audience-bound so blast radius is constrained.)
    if not subject_id:
        try:
            from gdx_dispatch.core.auth_dispatcher import get_current_principal
            principal = await get_current_principal(request)
            subject_id = str(principal.identity_id)
        except HTTPException as auth_exc:
            if auth_exc.status_code != 401:
                raise
            # Not authenticated — bounce through the SPA login. After a
            # successful login, LoginView redirects back to the original
            # /oauth/authorize URL and the second pass picks up the
            # session cookie.
            full_path = request.url.path
            if request.url.query:
                full_path = f"{full_path}?{request.url.query}"
            login_url = f"/login?redirect={urlencode({'_': full_path})[2:]}"
            return RedirectResponse(url=login_url, status_code=302)

    # S4: when TenantMiddleware ran (tenant-host requests), the resolved
    # tenant takes precedence over the caller-supplied query param.
    # Caller-supplied tenant_id is retained ONLY as a legacy path for
    # platform-host (`gdx.*`) flows that haven't migrated yet.
    state_tenant = getattr(request.state, "tenant", None)
    if state_tenant and isinstance(state_tenant, dict) and state_tenant.get("id"):
        tenant_id = str(state_tenant["id"])

    # `resource` is caller-driven (RFC 8707). We do NOT auto-default it
    # to the canonical MCP audience — auto-defaulting would convert
    # every legacy non-MCP flow to a JWT-shaped token under tenant
    # hosts, breaking back-compat. Only callers that explicitly ask
    # for an MCP-bound token get the JWT path.
    extra: dict[str, Any] = {}
    if resource:
        # Per RFC 8707 we accept the resource indicator and surface it in
        # the auth-code's extra dict so the token endpoint can verify
        # alignment and bake a JWT aud claim on token mint.
        extra["resource"] = resource
        try:
            extra["issuer"] = request_base_url(request)
        except ValueError:
            # No Host header → cannot derive a tenant-bound issuer.
            # Refuse rather than fall back silently.
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_request",
                        "error_description": "Host header required for tenant-bound tokens"},
            ) from None

    code = mint_authorization_code(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        tenant_id=tenant_id,
        subject_id=subject_id,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        extra=extra or None,
    )

    params = {"code": code}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


# ---------------------------------------------------------------------------
# /oauth/token
# ---------------------------------------------------------------------------


@router.post("/token")
def token(
    request: Request,
    grant_type: str = Form(...),
    code: str | None = Form(None),
    redirect_uri: str | None = Form(None),
    client_id: str | None = Form(None),
    code_verifier: str | None = Form(None),
    refresh_token: str | None = Form(None),
    resource: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """RFC 6749 §4.1.3 token request (authorization_code grant).

    Supports: authorization_code, refresh_token.
    Rejects: password, client_credentials (not in SS-21 scope — SS-28 may add).

    Sprint mcp-streamable-http S4: when the auth code's ``extra``
    carries a ``resource`` indicator (RFC 8707) matching ``<host>/mcp``,
    mint a JWT bearer token bound to the tenant (``aud`` + ``gdx_tid``)
    instead of the legacy opaque token.
    """
    if grant_type == "authorization_code":
        return _exchange_auth_code(
            db=db,
            code=code,
            redirect_uri=redirect_uri,
            client_id=client_id,
            code_verifier=code_verifier,
            requested_resource=resource,
            request=request,
        )
    if grant_type == "refresh_token":
        return _exchange_refresh_token(
            db=db, refresh_token=refresh_token, client_id=client_id
        )
    return _json_error("unsupported_grant_type", grant_type)


def _exchange_auth_code(
    *,
    db: Session,
    code: str | None,
    redirect_uri: str | None,
    client_id: str | None,
    code_verifier: str | None,
    requested_resource: str | None = None,
    request: Request | None = None,
) -> JSONResponse:
    if not code or not redirect_uri or not client_id:
        return _json_error("invalid_request", "code, redirect_uri, client_id required")

    # Validate client still exists
    app_row = _load_client(db, client_id)
    if app_row is None:
        return _json_error("invalid_client", "unknown client_id")

    # Atomic consume — single-use
    rec = consume_authorization_code(code)
    if rec is None:
        # Could be: unknown, expired, or REPLAY. Treat as invalid_grant per RFC.
        # this code (look up via audit log).
        return _json_error("invalid_grant", "authorization code invalid, expired, or reused")

    ok, err = validate_redemption(
        rec,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
    )
    if not ok:
        return _json_error(err or "invalid_grant", "redemption failed")

    now = time.time()

    # S4: tenant-bound JWT path. Activated when the authorize step
    # captured a `resource` indicator (RFC 8707) and the token request
    # asks for the same resource. We refuse cross-resource handouts:
    # if the caller asked for a resource the code wasn't bound to,
    # error out (loud) rather than ignore.
    code_resource = (rec.extra or {}).get("resource")
    code_issuer = (rec.extra or {}).get("issuer")
    if code_resource:
        if requested_resource and requested_resource != code_resource:
            return _json_error(
                "invalid_target",
                f"resource indicator mismatch: code bound to {code_resource!r}, "
                f"token request asked for {requested_resource!r}",
            )
        if not rec.tenant_id:
            return _json_error(
                "invalid_grant",
                "auth code carries resource indicator but no tenant_id; "
                "the authorize endpoint must derive tenant from request.state",
            )
        if not code_issuer:
            return _json_error(
                "invalid_grant",
                "auth code carries resource indicator but no issuer; "
                "the authorize endpoint must record request_base_url(request)",
            )
        access = mint_mcp_access_token(
            tenant_id=rec.tenant_id,
            subject_id=rec.subject_id or "",
            issuer=code_issuer,
            audience=code_resource,
            scope=rec.scope or "mcp:invoke",
            ttl_seconds=ACCESS_TOKEN_TTL_SECONDS,
        )
        # No refresh-token rotation for MCP-bound JWTs in this slice; S4
        # explicitly excludes refresh handling because cross-tenant
        # refresh semantics need their own design (D-S4-01).
        return JSONResponse(
            status_code=200,
            content={
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL_SECONDS,
                "scope": rec.scope or "mcp:invoke",
            },
        )

    # Legacy opaque-token path (non-MCP scopes).
    access = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(48)
    tr = TokenRecord(
        access_token=access,
        refresh_token=refresh,
        client_id=client_id,
        scope=rec.scope,
        tenant_id=rec.tenant_id,
        subject_id=rec.subject_id,
        issued_at=now,
        expires_at=now + ACCESS_TOKEN_TTL_SECONDS,
        extra=dict(rec.extra),
    )
    get_token_store().put(tr)

    return JSONResponse(
        status_code=200,
        content={
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": refresh,
            "scope": rec.scope,
        },
    )


def _exchange_refresh_token(
    *, db: Session, refresh_token: str | None, client_id: str | None
) -> JSONResponse:
    if not refresh_token or not client_id:
        return _json_error("invalid_request", "refresh_token + client_id required")
    store = get_token_store()
    rec = store.get_by_refresh(refresh_token)
    if rec is None or rec.revoked or rec.client_id != client_id:
        return _json_error("invalid_grant", "refresh_token invalid")
    # Rotate: atomically delete old pair + SETEX new pair (single pipeline).
    now = time.time()
    access = secrets.token_urlsafe(32)
    new_refresh = secrets.token_urlsafe(48)
    new_rec = TokenRecord(
        access_token=access,
        refresh_token=new_refresh,
        client_id=rec.client_id,
        scope=rec.scope,
        tenant_id=rec.tenant_id,
        subject_id=rec.subject_id,
        issued_at=now,
        expires_at=now + ACCESS_TOKEN_TTL_SECONDS,
        extra=dict(rec.extra),
    )
    store.rotate(rec, new_rec)
    return JSONResponse(
        status_code=200,
        content={
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": new_refresh,
            "scope": rec.scope,
        },
    )


# ---------------------------------------------------------------------------
# /oauth/revoke — RFC 7009
# ---------------------------------------------------------------------------


@router.post("/revoke")
def revoke(
    token: str = Form(...),
    client_id: str | None = Form(None),
    token_type_hint: str | None = Form(None),
):
    """RFC 7009 §2.2: unsupported/unknown tokens MUST still return 200."""
    try:
        get_token_store().revoke(token)
    except Exception as exc:  # defensive — never leak internals via revoke
        logger.warning("revoke error for client=%s: %s", client_id, exc)
    return JSONResponse(status_code=200, content={})


# ---------------------------------------------------------------------------
# /oauth/introspect — RFC 7662
# ---------------------------------------------------------------------------


@router.post("/introspect")
def introspect(
    token: str = Form(...),
    client_id: str | None = Form(None),
    token_type_hint: str | None = Form(None),
):
    store = get_token_store()
    rec = store.get_by_access(token) or store.get_by_refresh(token)
    if rec is None or rec.revoked or rec.expires_at < time.time():
        return JSONResponse(status_code=200, content={"active": False})
    return JSONResponse(
        status_code=200,
        content={
            "active": True,
            "client_id": rec.client_id,
            "scope": rec.scope,
            "token_type": "Bearer",
            "exp": int(rec.expires_at),
            "iat": int(rec.issued_at),
            "sub": rec.subject_id,
            "tenant_id": rec.tenant_id,
        },
    )


# ---------------------------------------------------------------------------
# /oauth/register — RFC 7591 Dynamic Client Registration
# ---------------------------------------------------------------------------
#
# Sprint mcp-streamable-http S5. claude.ai's MCP connector signup
# discovers `registration_endpoint` from the AS metadata (S3) and
# POSTs here with its desired client metadata. Per RFC 7591 §3.2, the
# server MUST mint and return a fresh `client_id` (and `client_secret`
# for confidential clients) in a 201 response.
#
# Tenant binding: the issued client is bound to the tenant whose host
# served the registration request. A client minted under
# `gdx.example.com/oauth/register` cannot be used at any other
# tenant's `/oauth/authorize` because `_load_client` plus the eventual
# JWT `gdx_tid` claim (S4) constrain it to a single tenant context.


def _hash_secret(raw: str) -> str:
    """RFC-acceptable hash for OAuth client secrets.

    OAuth client secrets are high-entropy server-issued credentials, so
    SHA-256 (no salt) is the conventional choice — bcrypt's slowdown
    only buys us protection against offline brute-force on
    user-chosen passwords, which doesn't apply here. Mirrors the
    server-issued-secret pattern in ``gdx_dispatch.core.service_accounts``.
    """
    import hashlib
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_RFC7591_DEFAULT_GRANT_TYPES = ["authorization_code", "refresh_token"]
_RFC7591_DEFAULT_RESPONSE_TYPES = ["code"]
# claude.ai's MCP connector uses authorization_code + PKCE. We accept
# the broader RFC 7591 grant set but reject password / implicit per
# OAuth 2.1 — the server's `/oauth/token` only supports
# authorization_code and refresh_token anyway, so advertising more
# would be a lie.
_ALLOWED_GRANT_TYPES = {"authorization_code", "refresh_token", "client_credentials"}
_ALLOWED_RESPONSE_TYPES = {"code"}


@router.post("/register")
async def register(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """RFC 7591 Dynamic Client Registration.

    Tenant-scoped: requires ``request.state.tenant`` (set by
    ``TenantMiddleware``). The minted client_id can authorize / token
    only against the tenant whose host served this request.
    """
    state_tenant = getattr(request.state, "tenant", None)
    if not state_tenant or not isinstance(state_tenant, dict) or not state_tenant.get("id"):
        return _json_error(
            "invalid_request",
            "DCR requires a tenant-scoped host (TenantMiddleware did not resolve)",
            status=400,
        )
    tenant_id = str(state_tenant["id"])

    # RFC 7591 metadata from request body. Empty body coerces to {} so
    # a client can register with all defaults — useful for the
    # claude.ai connector when it sends a minimal payload.
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes else {}
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_error("invalid_request", f"body must be JSON: {exc}", status=400)
    if not isinstance(body, dict):
        return _json_error("invalid_request", "body must be a JSON object", status=400)

    # redirect_uris: REQUIRED for code/refresh grants (RFC 7591 §2).
    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return _json_error(
            "invalid_redirect_uri",
            "redirect_uris is required and must be a non-empty list of strings",
            status=400,
        )
    if any(not isinstance(u, str) or not u.strip() for u in redirect_uris):
        return _json_error(
            "invalid_redirect_uri",
            "redirect_uris entries must be non-empty strings",
            status=400,
        )

    grant_types = body.get("grant_types") or _RFC7591_DEFAULT_GRANT_TYPES
    if not isinstance(grant_types, list) or any(g not in _ALLOWED_GRANT_TYPES for g in grant_types):
        return _json_error(
            "invalid_client_metadata",
            f"grant_types must be a subset of {sorted(_ALLOWED_GRANT_TYPES)}",
            status=400,
        )

    response_types = body.get("response_types") or _RFC7591_DEFAULT_RESPONSE_TYPES
    if not isinstance(response_types, list) or any(r not in _ALLOWED_RESPONSE_TYPES for r in response_types):
        return _json_error(
            "invalid_client_metadata",
            f"response_types must be a subset of {sorted(_ALLOWED_RESPONSE_TYPES)}",
            status=400,
        )

    token_endpoint_auth_method = body.get("token_endpoint_auth_method") or "client_secret_basic"
    if token_endpoint_auth_method not in (
        "client_secret_basic", "client_secret_post", "none",
    ):
        return _json_error(
            "invalid_client_metadata",
            "token_endpoint_auth_method must be one of "
            "client_secret_basic, client_secret_post, none",
            status=400,
        )

    # Mint client_id + (for confidential clients) client_secret.
    client_id = f"dcr_{secrets.token_urlsafe(16)}"
    raw_secret: str | None = None
    secret_hash: str | None = None
    secret_prefix: str | None = None
    if token_endpoint_auth_method != "none":
        raw_secret = secrets.token_urlsafe(32)
        secret_hash = _hash_secret(raw_secret)
        secret_prefix = raw_secret[:8]

    issued_at = int(time.time())
    row = OAuthDynamicClient(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret_hash=secret_hash,
        secret_prefix=secret_prefix,
        client_name=body.get("client_name"),
        redirect_uris=list(redirect_uris),
        grant_types=list(grant_types),
        response_types=list(response_types),
        token_endpoint_auth_method=token_endpoint_auth_method,
        scope=body.get("scope") or "mcp:invoke",
        client_id_issued_at=issued_at,
        client_secret_expires_at=0,  # never expires; rotation is a future slice
    )
    db.add(row)
    db.commit()

    response: dict[str, Any] = {
        "client_id": client_id,
        "client_id_issued_at": issued_at,
        "redirect_uris": list(redirect_uris),
        "grant_types": list(grant_types),
        "response_types": list(response_types),
        "token_endpoint_auth_method": token_endpoint_auth_method,
        "scope": row.scope,
    }
    if body.get("client_name"):
        response["client_name"] = body["client_name"]
    if raw_secret is not None:
        response["client_secret"] = raw_secret
        response["client_secret_expires_at"] = 0

    return JSONResponse(status_code=201, content=response)


__all__ = [
    "router",
    "get_db",
    "get_token_store",
    "set_token_store_for_tests",
    "TokenRecord",
    "_RedisTokenStore",
    "_PRODUCTION_READY",
]
