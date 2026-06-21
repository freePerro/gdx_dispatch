from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import ExpiredSignatureError
from jwt.exceptions import InvalidTokenError as JWTError
from pydantic import BaseModel, Field
from redis import from_url
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.auth_revoke import revoke_user_sessions
from gdx_dispatch.core.contexts import execution_context
from gdx_dispatch.core.database import get_db, get_db
from gdx_dispatch.core.denylist import Denylist
from gdx_dispatch.core.modules import require_role

log = logging.getLogger(__name__)

# Platform-level email (sent from CC, not tenant)
PLATFORM_SMTP_HOST = os.getenv("PLATFORM_SMTP_HOST", "smtp.titan.email")
PLATFORM_SMTP_PORT = int(os.getenv("PLATFORM_SMTP_PORT", "465"))
PLATFORM_SMTP_USER = os.getenv("PLATFORM_SMTP_USER", "info@example.com")
PLATFORM_SMTP_PASS = os.getenv("PLATFORM_SMTP_PASS", "")
PLATFORM_FROM_NAME = os.getenv("PLATFORM_FROM_NAME", "DispatchApp")


def _send_platform_email(to_email: str, subject: str, html_body: str) -> None:
    """Send email from the platform (CC) account, not tenant."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not PLATFORM_SMTP_PASS:
        log.warning("PLATFORM_SMTP_PASS not set — email not sent to %s", to_email)
        return

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{PLATFORM_FROM_NAME} <{PLATFORM_SMTP_USER}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    s = smtplib.SMTP_SSL(PLATFORM_SMTP_HOST, PLATFORM_SMTP_PORT, timeout=15)
    s.login(PLATFORM_SMTP_USER, PLATFORM_SMTP_PASS)
    s.sendmail(PLATFORM_SMTP_USER, to_email, msg.as_string())
    s.quit()
    log.info("platform_email_sent to=%s subject=%s", to_email, subject)


router = APIRouter(prefix="/auth", tags=["auth"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

# SS-7 Slice H — app-scoped denylist lifecycle seam. The denylist lives on
# ``request.app.state`` so the revoke endpoint (writer) and ``get_current_user``
# (reader) resolve to the *same instance* for any given FastAPI app, while
# remaining naturally isolated between independent app instances (one per
# test, one per worker process at runtime).
#
# SS-7 Slice J — the lazy-create path now also attaches an optional Redis
# client, turning the app-scoped denylist into a best-effort cross-worker
# cache. The Slice H within-app identity contract is preserved exactly
# (same ``app.state.denylist`` instance per app). The Redis seam is pulled
# from :func:`_denylist_redis_client` so tests can monkeypatch a fake
# client in without touching a real Redis server — and so a missing /
# unreachable ``REDIS_URL`` degrades silently to the prior Slice H
# local-only behavior instead of crashing the revoke path.
#
# SS-7 Slice K — explicit operator control over the backend via
# ``DENYLIST_BACKEND_MODE``. The env var is parsed here at the router
# seam so the :class:`Denylist` core class stays adapter-agnostic (a
# regression that moves mode parsing into :mod:`gdx_dispatch.core.denylist` is
# pinned shut by a dedicated test). See :func:`_denylist_redis_client`
# below for the full mode matrix.
_DENYLIST_BACKEND_MODE_ENV = "DENYLIST_BACKEND_MODE"
_DENYLIST_BACKEND_MODES: frozenset[str] = frozenset({"memory", "redis"})


def _denylist_redis_client() -> Any | None:
    """Return a Redis client for cross-worker denylist fan-out, or ``None``.

    Mode matrix (``DENYLIST_BACKEND_MODE``, case-insensitive, surrounding
    whitespace stripped):

    * ``memory`` — explicit local-only opt-out. ``REDIS_URL`` is ignored
      even if set; the attached :class:`Denylist` falls back to the
      Slice C / Slice H local-only behavior byte-for-byte.
    * ``redis`` — explicit Redis mode. Requires ``REDIS_URL``; when it
      is missing/blank or the client cannot be constructed, a warning
      is logged and the helper returns ``None`` (fail-open to local).
    * unset / blank — Slice J default: attempt Redis iff ``REDIS_URL``
      is set, otherwise return ``None``. This is the current production
      path and is preserved byte-for-byte by this slice.
    * any other value — log a warning and degrade to the unset default
      so an operator typo (``rediss``, ``REDI$``) never silently
      disables cross-worker fan-out for a deployment whose ``REDIS_URL``
      is otherwise valid.

    Constructing a ``redis.Redis`` via ``from_url`` does NOT open a
    connection; the connection is lazy (first command triggers it). An
    error here would therefore be a configuration mishap — URL scheme,
    bad TLS options, etc. — and we treat every branch as fail-open so a
    broken ``REDIS_URL`` or a typoed mode cannot block app startup or
    the revoke endpoint.
    """
    mode_raw = os.getenv(_DENYLIST_BACKEND_MODE_ENV, "")
    mode = mode_raw.strip().lower()

    if mode == "memory":
        return None

    url = os.getenv("REDIS_URL", "").strip()

    if mode == "redis":
        if not url:
            log.warning(
                "denylist_backend_mode_redis_missing_redis_url: "
                "DENYLIST_BACKEND_MODE=redis but REDIS_URL is unset; "
                "falling back to local-only"
            )
            return None
        try:
            return from_url(url, decode_responses=True)
        except Exception:  # intentional fail-open behavior for configuration errors to prevent blocking app startup
            log.exception("denylist_redis_client_build_failed")
            return None

    if mode and mode not in _DENYLIST_BACKEND_MODES:
        # Truncate the echoed value so a pathological env var cannot blow
        # up a log line; 64 chars is enough to diagnose the typo.
        log.warning(
            "denylist_backend_mode_invalid: mode=%r; "
            "falling back to unset-default behavior",
            mode_raw[:64],
        )

    # Unset / blank / invalid → Slice J default.
    if not url:
        return None
    try:
        return from_url(url, decode_responses=True)
    except Exception:  # intentional fail-open behavior for configuration errors to prevent blocking app startup
        log.exception("denylist_redis_client_build_failed")
        return None


def _get_app_denylist(request: Request) -> Denylist:
    """Return the denylist attached to ``request.app.state``, creating it lazily.

    The first request to touch an app gets a fresh :class:`Denylist` stashed
    on ``app.state``; every subsequent request on the same app sees the same
    object (identity preserved). This is the single point of denylist
    resolution — both the admin revoke writer and the auth reader call it
    so they cannot drift onto separate instances.

    Slice J — at lazy-create time we attach a Redis client (or ``None``)
    via :func:`_denylist_redis_client`. Once the :class:`Denylist` is
    stashed on ``app.state``, the Redis client is bound to that instance
    for the app's lifetime; subsequent calls on the same app return the
    exact same object without rebuilding the Redis client.
    """
    denylist = getattr(request.app.state, "denylist", None)
    if denylist is None:
        denylist = Denylist(redis_client=_denylist_redis_client())
        request.app.state.denylist = denylist
    return denylist
redis = from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
ACCESS_TTL, REFRESH_TTL = 15 * 60, 30 * 24 * 60 * 60
# F-90 / 2026-04-29 — refresh-token rotation hardening (banking-grade).
# OAuth 2.1 / RFC 9700 / FAPI 2.0 require revoke-on-first-reuse. The
# leeway exists because the legitimate cause of "same refresh token
# arrives twice" is a network race where the response to a previous
# refresh was lost and the client retried. 30s mirrors Auth0/Okta
# defaults. Outside the window, any reuse revokes the entire family.
REFRESH_REPLAY_LEEWAY_SECONDS = 30
REFRESH_REPLAY_CACHE_TTL = 60  # must exceed leeway so cache outlives the window
PRIV = os.getenv("RS_PRIVATE_KEY", "").replace("\\n", "\n").strip()
PUB = os.getenv("RS_PUBLIC_KEY", "").replace("\\n", "\n").strip()
_JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
ALG = "RS256" if PRIV else "HS256"
SIGN_KEY = PRIV or _JWT_SECRET
VERIFY_KEY = (PUB or PRIV) if ALG == "RS256" else SIGN_KEY

# Refuse to start if no valid signing key is configured. Prior to 2026-04-12
# the code fell through to a 10-byte "dev-secret" literal, which meant
# production silently signed every JWT with an attacker-guessable value.
# If you're seeing this error: set RS_PRIVATE_KEY+RS_PUBLIC_KEY (preferred)
# or JWT_SECRET (at least 32 bytes) before starting the app.
if not SIGN_KEY:
    raise RuntimeError(
        "JWT signing key not configured. Set RS_PRIVATE_KEY+RS_PUBLIC_KEY "
        "(preferred, RS256) or JWT_SECRET (≥32 bytes, HS256). The app "
        "will not start with an insecure default."
    )
if ALG == "HS256" and len(SIGN_KEY) < 32:
    raise RuntimeError(
        f"JWT_SECRET is only {len(SIGN_KEY)} bytes — HS256 requires at "
        "least 32 bytes per RFC 7518. Use a longer secret or configure "
        "RS_PRIVATE_KEY+RS_PUBLIC_KEY for RS256."
    )

class LoginBody(BaseModel):
    # Bounds prevent DoS from arbitrarily large password submissions and
    # reject obviously-invalid emails at the parser level. 254 is the RFC 5321
    # local+domain max; 128 is a generous bcrypt-safe password ceiling.
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)

def _unauth(msg: str = "Invalid credentials") -> HTTPException:
    return HTTPException(status_code=401, detail=msg)

def _issue(sub: str, tenant_id: str, role: str, ttl: int, typ: str) -> tuple[str, str]:
    c = {
        "sub": sub, "tenant_id": tenant_id, "role": role, "jti": str(uuid4()), "typ": typ,
        "exp": int((datetime.now(UTC) + timedelta(seconds=ttl)).timestamp()),
    }
    return jwt.encode(c, SIGN_KEY, algorithm=ALG), str(c["jti"])

@router.post("/login")
def login(body: LoginBody, request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    from gdx_dispatch.models.tenant_models import User as _User
    _u = db.execute(select(_User).where(_User.email == body.email)).scalars().first()

    # Account-status gate: reject soft-deleted, deactivated, or locked users
    # BEFORE password verification. Without this check a soft-deleted user
    # whose row was hidden from list endpoints could still authenticate and
    # mint tokens (Michael Tallman incident, 2026-04-29 — three login_success
    # events on a row with deleted_at set since 2026-04-08).
    block_reason: str | None = None
    if _u is not None:
        if getattr(_u, "deleted_at", None) is not None:
            block_reason = "user_deleted"
        elif getattr(_u, "active", True) is False:
            block_reason = "user_inactive"
        else:
            locked_until = getattr(_u, "locked_until", None)
            if locked_until is not None and locked_until > datetime.now(UTC):
                block_reason = "user_locked"
    if block_reason:
        try:
            log_audit_event_sync(
                db,
                tenant_id=str(getattr(request.state, "tenant", {}).get("id", "")),
                user_id=str(_u.id) if _u else "anonymous",
                action="login_blocked",
                entity_type="auth",
                entity_id=body.email,
                details={"email": body.email, "reason": block_reason},
                request=request,
            )
            db.commit()
        except Exception:
            log.exception("login_blocked_audit_error")
        raise _unauth()

    row = {"id": str(_u.id), "password_hash": _u.password_hash, "role": _u.role} if _u else None
    try:
        import bcrypt as _bcrypt
        ok = False
        if row:
            pw_hash = str(row["password_hash"])
            if pw_hash.startswith("$2"):
                ok = _bcrypt.checkpw(body.password.encode(), pw_hash.encode())
            elif pw_hash.startswith("pbkdf2:") or pw_hash.startswith("scrypt:"):
                from werkzeug.security import check_password_hash
                ok = check_password_hash(pw_hash, body.password)
    except (ImportError, RuntimeError, ValueError) as e:
        log.exception("password_verifier_unavailable")
        raise HTTPException(status_code=500, detail=f"Password verifier unavailable: {e}") from e
    if not ok:
        # Failed login attempt — audit BEFORE raising so the forensic trail
        # captures the attempted email and source IP even on invalid creds.
        try:
            log_audit_event_sync(
                db,
                tenant_id=str(getattr(request.state, "tenant", {}).get("id", "")),
                user_id="anonymous",
                action="login_failed",
                entity_type="auth",
                entity_id=body.email,
                details={"email": body.email, "reason": "invalid_credentials"},
                request=request,
            )
            db.commit()
        except Exception:
            log.exception("login_failed_audit_error")
        raise _unauth()
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    sub, role = str(row["id"]), str(row.get("role") or "user")
    # Bookkeep last successful login so the Settings → Users panel can
    # show a real timestamp instead of "Never" for every active user.
    # Pre-fix only the portal login path bumped this (routers/portal.py).
    try:
        _u.last_login_at = datetime.now(UTC)
    except Exception:
        log.exception("last_login_bookkeep_failed")
    access, _ = _issue(sub, tenant_id, role, ACCESS_TTL, "access")
    refresh, rjti = _issue(sub, tenant_id, role, REFRESH_TTL, "refresh")
    redis.sadd(f"refresh_family:{sub}", rjti); redis.expire(f"refresh_family:{sub}", REFRESH_TTL)  # noqa: E701,E702
    log.info("auth_login_success", extra={"tenant_id": tenant_id, "user_id": sub})
    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=sub,
            action="login_success",
            entity_type="auth",
            entity_id=sub,
            details={"email": body.email, "role": role},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("login_success_audit_error")
    # Surface a small user shape on the login response so the SPA's
    # auth store can populate Topbar avatar / user menu / display name
    # without a second round-trip. The SPA stores this under user.value;
    # downstream surfaces (`auth.user?.name`) read from here. No PII
    # beyond what every authenticated request would already see.
    user_payload = {
        "id": sub,
        "email": body.email,
        "name": (_u.full_name or _u.name or body.email) if _u is not None else body.email,
        "role": role,
    }
    resp = JSONResponse({
        "access_token": access,
        "token_type": "bearer",
        "user": user_payload,
    })
    # Cookies are scoped to .example.com so the same session is valid
    # across every tenant subdomain AND the platform host (app.*). This is a
    # load-bearing invariant: duplicate host-only cookies would shadow the
    # domain-wide one (RFC 6265 §5.4 orders by path then creation time, NOT
    # domain specificity), so EVERY set/delete_cookie in this module must use
    # the same domain. SameSite=lax on the refresh cookie (was strict) so it
    # survives the platform-host hand-off redirect from app.* to <slug>.*.
    resp.set_cookie(
        "refresh_token", refresh,
        httponly=True, secure=True, samesite="lax",
        domain=".example.com", max_age=REFRESH_TTL,
    )
    # access_token cookie powers the /oauth/authorize bridge for claude.ai
    # connectors. HttpOnly + Secure; SPA continues to use the JSON-body
    # access_token for its own API calls (Bearer header).
    resp.set_cookie(
        "access_token", access,
        httponly=True, secure=True, samesite="lax",
        domain=".example.com", max_age=ACCESS_TTL,
    )
    return resp


def _lookup_memberships_by_email(control_db: Session, email_norm: str) -> list[Any]:
    """Return one row per active (identity, membership, tenant) triple.

    Calls the ``tenant_login_lookup`` SECURITY DEFINER function (migration
    072) which bypasses the RLS policy on ``memberships`` so an
    unauthenticated platform-host caller can resolve their own tenants
    by email. Tests monkeypatch this seam; SQLite has no SECURITY DEFINER
    equivalent.

    Each returned row exposes ``identity_id``, ``tenant_id``, ``slug``,
    ``name``, ``role`` as attributes.
    """
    from sqlalchemy import text as sa_text
    return list(control_db.execute(
        sa_text(
            "SELECT identity_id, tenant_id, slug, name, role "
            "FROM tenant_login_lookup(:e)"
        ),
        {"e": email_norm},
    ).all())


def _open_tenant_session_for_login(tenant) -> tuple[Any, Any]:
    """Open a short-lived (engine, Session) pair against the application database.

    Single-tenant: always the one application DB. The ``tenant`` argument is
    accepted for interface compatibility with tests that monkeypatch this seam.
    Returns (engine, session); the caller is responsible for closing both.
    """
    from gdx_dispatch.core.database import SessionLocal, app_engine
    return app_engine, SessionLocal()


class PlatformLoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    # Optional — set by SPA on the second POST after the user picks a tenant
    # in the multi-tenant case. UUID string.
    tenant_id: str | None = Field(default=None, max_length=64)


@router.post("/platform-login")
def platform_login(
    body: PlatformLoginBody,
    request: Request,
    control_db: Session = Depends(get_db),
) -> JSONResponse:
    """Email-first login for the tenant-agnostic platform host.

    Flow:
      1. Look up Identity by email in the control plane.
      2. Resolve active Memberships → Tenants.
      3. If 0 tenants → 401.
      4. If N>1 tenants and body.tenant_id not provided → return picker
         payload (no token issued, no password verified yet).
      5. Otherwise (1 tenant, or N tenants with explicit choice): open that
         tenant's DB, verify password against the per-tenant `users` row,
         mint JWT scoped to that tenant, return access token + redirect_url.

    The per-tenant /auth/login endpoint is unchanged and remains the path
    used when the user is already on `<slug>.example.com`.
    """
    from sqlalchemy import func
    from types import SimpleNamespace
    from gdx_dispatch.models.tenant_models import User as _User

    email_norm = body.email.strip().lower()
    rows = _lookup_memberships_by_email(control_db, email_norm)
    if not rows:
        # Same response shape as bad password — never confirm/deny email.
        raise _unauth()

    # If multiple memberships and the SPA hasn't picked one yet, show picker.
    if body.tenant_id:
        match = next((r for r in rows if str(r.tenant_id) == body.tenant_id), None)
        if match is None:
            raise _unauth()
        chosen = match
    else:
        chosen = rows[0]
    tenant = SimpleNamespace(
        id=chosen.tenant_id, slug=chosen.slug, name=chosen.name,
    )
    membership = SimpleNamespace(role=chosen.role)

    # Open tenant DB, verify password against the per-tenant user row.
    try:
        eng, tenant_session = _open_tenant_session_for_login(tenant)
    except RuntimeError:
        log.error("platform_login_missing_db_url tenant_id=%s", tenant.id)
        raise _unauth()
    try:
        try:
            user = tenant_session.execute(
                select(_User).where(func.lower(_User.email) == email_norm)
            ).scalars().first()
            if user is None or getattr(user, "deleted_at", None) is not None:
                raise _unauth()
            if getattr(user, "active", True) is False:
                raise _unauth()
            locked_until = getattr(user, "locked_until", None)
            if locked_until is not None and locked_until > datetime.now(UTC):
                raise _unauth()

            ok = False
            try:
                import bcrypt as _bcrypt
                pw_hash = str(user.password_hash)
                if pw_hash.startswith("$2"):
                    ok = _bcrypt.checkpw(body.password.encode(), pw_hash.encode())
                elif pw_hash.startswith("pbkdf2:") or pw_hash.startswith("scrypt:"):
                    from werkzeug.security import check_password_hash
                    ok = check_password_hash(pw_hash, body.password)
            except (ImportError, RuntimeError, ValueError) as e:
                log.exception("password_verifier_unavailable")
                raise HTTPException(
                    status_code=500, detail=f"Password verifier unavailable: {e}"
                ) from e
            if not ok:
                raise _unauth()

            tenant_id_str = str(tenant.id)
            sub = str(user.id)
            role = str(getattr(user, "role", None) or membership.role or "user")
            access, _ = _issue(sub, tenant_id_str, role, ACCESS_TTL, "access")
            refresh, rjti = _issue(sub, tenant_id_str, role, REFRESH_TTL, "refresh")
            redis.sadd(f"refresh_family:{sub}", rjti)
            redis.expire(f"refresh_family:{sub}", REFRESH_TTL)

            log.info(
                "platform_login_success tenant_id=%s user_id=%s",
                tenant_id_str, sub,
            )
            user_payload = {
                "id": sub,
                "email": body.email,
                "name": (user.full_name or user.name or body.email),
                "role": role,
            }
            resp = JSONResponse({
                "access_token": access,
                "token_type": "bearer",
                "user": user_payload,
                "tenant": {
                    "id": tenant_id_str,
                    "slug": tenant.slug,
                    "name": tenant.name,
                },
                "redirect_url": f"https://{tenant.slug}.example.com/dashboard",
            })
            # Scope cookies to .example.com so the destination tenant
            # subdomain (gdx.example.com etc.) can refresh against the
            # same session after the platform-host hand-off. SameSite=lax so
            # the refresh cookie survives the top-level navigation from app.*
            # to <tenant>.*.
            resp.set_cookie(
                "refresh_token", refresh,
                httponly=True, secure=True, samesite="lax",
                domain=".example.com", max_age=REFRESH_TTL,
            )
            resp.set_cookie(
                "access_token", access,
                httponly=True, secure=True, samesite="lax",
                domain=".example.com", max_age=ACCESS_TTL,
            )
            return resp
        finally:
            tenant_session.close()
    finally:
        eng.dispose()


@router.post("/refresh")
def refresh(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    token = request.cookies.get("refresh_token")
    if not token:
        raise _unauth("Missing refresh token")
    try:
        c = jwt.decode(token, VERIFY_KEY, algorithms=[ALG])
        if c.get("typ") != "refresh":
            raise _unauth("Invalid refresh token")
        old_jti, sub = str(c["jti"]), str(c["sub"])
    except (JWTError, KeyError, TypeError, ValueError) as exc:
        log.exception("auth_refresh_failed")
        raise _unauth("Invalid or expired refresh token") from exc
    tid_for_audit = str(c.get("tenant_id", ""))

    # D-S119-refresh-denylist-gap (Doug 2026-05-10):
    # Pre-fix, /auth/admin/revoke wrote the jti to the SS-7 denylist but
    # this endpoint never consulted it — so the user whose access-token
    # jti was "revoked" could just hit /auth/refresh and mint a fresh
    # access token within ≤15 min. Auditor named this the keystone.
    #
    # Fix: check the denylist BEFORE the replay-detection branch. If the
    # refresh jti is administratively revoked, kill the entire family
    # via the shared revoke_user_sessions helper (RFC 9700 §refresh-token:
    # "the authorization server SHOULD also invalidate all access tokens
    # based on the same authorization grant") and 401. The denylist check
    # fires before replay detection because the two threat models are
    # different — forensic audit trail must reflect "admin revoke" vs
    # "replay attack" accurately.
    if _get_app_denylist(request).contains(old_jti):
        revoked_count = revoke_user_sessions(sub, reason="refresh_denylist_hit")
        try:
            log_audit_event_sync(
                db,
                tenant_id=tid_for_audit,
                user_id=sub,
                action="refresh_denied_token_revoked",
                entity_type="auth",
                entity_id=sub,
                details={
                    "jti": old_jti,
                    "family_revoked": True,
                    "sessions_revoked": revoked_count,
                },
                request=request,
            )
            db.commit()
        except Exception:
            log.exception("refresh_denylist_audit_error")
        raise _unauth("Token revoked")

    if redis.sismember("used_refresh_jtis", old_jti):
        # F-90 / 2026-04-29 — banking-grade replay handling.
        # Within the leeway window, the same JTI being re-presented is
        # almost certainly a network race (client lost the response and
        # retried). Re-emit the cached pair, no revoke.
        cached = redis.hgetall(f"refresh_redemption:{old_jti}")
        if cached and cached.get("access") and cached.get("refresh"):
            ts = float(cached.get("ts") or 0)
            if (time.time() - ts) <= REFRESH_REPLAY_LEEWAY_SECONDS:
                # Audit the replay attempt (SOC 2) but don't revoke.
                try:
                    log_audit_event_sync(
                        db, tenant_id=tid_for_audit, user_id=sub,
                        action="refresh_replay_within_leeway",
                        entity_type="auth", entity_id=sub,
                        details={"jti": old_jti, "leeway_s": REFRESH_REPLAY_LEEWAY_SECONDS},
                        request=request,
                    )
                    db.commit()
                except Exception:
                    log.exception("refresh_replay_audit_error")
                resp = JSONResponse({"access_token": cached["access"], "token_type": "bearer"})
                resp.set_cookie(
                    "refresh_token", cached["refresh"],
                    httponly=True, secure=True, samesite="lax",
                    domain=".example.com", max_age=REFRESH_TTL,
                )
                resp.set_cookie(
                    "access_token", cached["access"],
                    httponly=True, secure=True, samesite="lax",
                    domain=".example.com", max_age=ACCESS_TTL,
                )
                return resp

        # Outside the leeway — TRUE replay. Revoke the whole family.
        # OAuth 2.1 / FAPI 2.0 / RFC 9700 mandate. Add every JTI in
        # this user's refresh family to the used set, then drop the
        # family marker so subsequent refreshes from any sibling token
        # all 401.
        try:
            family_members = redis.smembers(f"refresh_family:{sub}") or set()
            if family_members:
                pipe = redis.pipeline()
                for jti in family_members:
                    pipe.sadd("used_refresh_jtis", jti)
                pipe.expire("used_refresh_jtis", REFRESH_TTL)
                pipe.delete(f"refresh_family:{sub}")
                pipe.execute()
        except Exception:
            log.exception("refresh_family_revoke_failed")

        try:
            log_audit_event_sync(
                db, tenant_id=tid_for_audit, user_id=sub,
                action="refresh_replay_detected",
                entity_type="auth", entity_id=sub,
                details={"jti": old_jti, "family_revoked": True},
                request=request,
            )
            db.commit()
        except Exception:
            log.exception("refresh_replay_audit_error")

        # Sink to F-18 server_errors so the admin dashboard surfaces it —
        # but ONCE per jti. A client that loops on a dead refresh cookie
        # (e.g. logout() not clearing the HttpOnly cookie pre-2026-05-14)
        # re-presents the same jti every poll tick; without this guard one
        # stuck tab wrote 1,526 identical rows and buried the real signal
        # (6,717 rows over 4 days). The first replay of a jti is signal;
        # the rest are noise from a client that hasn't been logged out
        # yet. SET NX is atomic so concurrent replays still sink exactly
        # once. Fail-open: a Redis hiccup records the error rather than
        # silently dropping a genuine replay attack.
        should_sink = True  # fail-open default: a Redis error still records
        try:
            should_sink = bool(
                redis.set(f"replay_sinked:{old_jti}", "1", nx=True, ex=REFRESH_TTL)
            )
        except Exception:
            log.exception("refresh_replay_sink_dedup_failed")
        if should_sink:
            try:
                from gdx_dispatch.modules.error_sink import record_server_error
                record_server_error(
                    request=request,
                    exc=Exception(f"refresh_replay_detected sub={sub} jti={old_jti}"),
                    status_code=401,
                    request_id=getattr(request.state, "request_id", None),
                )
            except Exception:
                log.exception("refresh_replay_sink_failed")

        raise _unauth("Refresh token replay detected — session revoked")

    redis.sadd("used_refresh_jtis", old_jti); redis.expire("used_refresh_jtis", REFRESH_TTL)  # noqa: E701,E702
    tid = str(c.get("tenant_id", ""))

    # Sprint Auth & Identity Hardening — Slice 1.
    # Pre-fix the role was lifted verbatim from the OLD refresh token. A
    # user demoted from admin → user in the DB kept minting `role:admin`
    # access tokens for the full REFRESH_TTL (days). A user soft-deleted
    # (`users.deleted_at IS NOT NULL`) likewise refreshed indefinitely.
    # DB is now the source of truth: lookup by `sub`, deny on
    # missing/deleted/inactive, derive the role from `users.role`.
    #
    # Service-account / PAT subjects are NOT in `users` — they're issued
    # via /api/pats with a different `sub` shape. Refresh isn't part of
    # the PAT flow (PATs are long-lived static tokens), so a missing row
    # under `/auth/refresh` is unambiguously a stale-or-forged human
    # session and the right response is 401.
    from gdx_dispatch.models.tenant_models import User as _RefreshUser

    user_row = db.execute(
        select(_RefreshUser).where(_RefreshUser.id == sub)
    ).scalar_one_or_none()
    if user_row is None or user_row.deleted_at is not None or user_row.active is False:
        # Revoke the family — every sibling refresh token now 401s too.
        try:
            family_members = redis.smembers(f"refresh_family:{sub}") or set()
            if family_members:
                pipe = redis.pipeline()
                for jti in family_members:
                    pipe.sadd("used_refresh_jtis", jti)
                pipe.expire("used_refresh_jtis", REFRESH_TTL)
                pipe.delete(f"refresh_family:{sub}")
                pipe.execute()
        except Exception:
            log.exception("refresh_family_revoke_on_db_verify_failed")
        try:
            log_audit_event_sync(
                db, tenant_id=tid, user_id=sub,
                action="refresh_denied_db_verify",
                entity_type="auth", entity_id=sub,
                details={
                    "reason": (
                        "user_missing" if user_row is None
                        else "user_deleted" if user_row.deleted_at is not None
                        else "user_inactive"
                    ),
                },
                request=request,
            )
            db.commit()
        except Exception:
            log.exception("refresh_denied_audit_error")
        raise _unauth("User no longer eligible to refresh")

    role = str(user_row.role or "user")
    access, _ = _issue(sub, tid, role, ACCESS_TTL, "access")
    refresh_new, new_jti = _issue(sub, tid, role, REFRESH_TTL, "refresh")
    redis.sadd(f"refresh_family:{sub}", new_jti); redis.expire(f"refresh_family:{sub}", REFRESH_TTL)  # noqa: E701,E702
    # F-90: cache the issued pair so a network-race retry within the
    # leeway window can be served idempotently instead of treated as
    # a replay attack.
    try:
        redis.hset(f"refresh_redemption:{old_jti}", mapping={
            "access": access, "refresh": refresh_new, "ts": str(time.time()),
        })
        redis.expire(f"refresh_redemption:{old_jti}", REFRESH_REPLAY_CACHE_TTL)
    except Exception:
        log.exception("refresh_redemption_cache_failed")
    try:
        log_audit_event_sync(
            db,
            tenant_id=tid,
            user_id=sub,
            action="token_refreshed",
            entity_type="auth",
            entity_id=sub,
            details={"role": role},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("token_refresh_audit_error")
    resp = JSONResponse({"access_token": access, "token_type": "bearer"})
    resp.set_cookie(
        "refresh_token", refresh_new,
        httponly=True, secure=True, samesite="lax",
        domain=".example.com", max_age=REFRESH_TTL,
    )
    # See login() for rationale on the SameSite=Lax access_token cookie.
    resp.set_cookie(
        "access_token", access,
        httponly=True, secure=True, samesite="lax",
        domain=".example.com", max_age=ACCESS_TTL,
    )
    return resp

@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    token = request.cookies.get("refresh_token")
    logout_sub = "anonymous"
    logout_tid = ""
    if token:
        try:
            c = jwt.decode(token, VERIFY_KEY, algorithms=[ALG], options={"verify_exp": False})
            redis.sadd("used_refresh_jtis", str(c.get("jti", ""))); redis.expire("used_refresh_jtis", REFRESH_TTL)  # noqa: E701,E702
            logout_sub = str(c.get("sub") or "anonymous")
            logout_tid = str(c.get("tenant_id", ""))
        except JWTError:
            log.exception("auth_logout_token_decode_failed")
    try:
        log_audit_event_sync(
            db,
            tenant_id=logout_tid,
            user_id=logout_sub,
            action="logout",
            entity_type="auth",
            entity_id=logout_sub,
            details={},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("logout_audit_error")
    # Cookies are domain-scoped (.example.com) so the delete must use
    # the same domain — without it Starlette emits a host-only Set-Cookie
    # Max-Age=0 which leaves the actual domain-wide cookie in place and the
    # user appears logged in on the next page load.
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("refresh_token", domain=".example.com")
    resp.delete_cookie("access_token", domain=".example.com")
    return resp

# ---------------------------------------------------------------------------
# Forgot / Reset Password (public — no auth required)
# ---------------------------------------------------------------------------

class ForgotPasswordBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)

class ResetPasswordBody(BaseModel):
    token: str = Field(min_length=10, max_length=100)
    new_password: str = Field(min_length=8, max_length=128)

@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordBody, request: Request, db: Session = Depends(get_db)):
    """Send a password reset link. Always returns success to prevent email enumeration."""
    import secrets
    email = body.email.strip()
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))

    # Rate limit: max 3 requests per email per hour
    rl_key = f"pw_reset_rl:{tenant_id}:{email.lower()}"
    # Sync redis client (see `redis = from_url(...)` above) — cast for mypy.
    attempts = cast(int, redis.incr(rl_key))
    if attempts == 1:
        redis.expire(rl_key, 3600)
    if attempts > 3:
        return {"ok": True, "message": "If an account exists with this email, a reset link has been sent."}

    from sqlalchemy import func as _func

    from gdx_dispatch.models.tenant_models import User as _User
    _u = db.execute(select(_User).where(_func.lower(_User.email) == email.lower())).scalars().first()
    row = {"id": str(_u.id), "email": _u.email} if _u else None

    if row:
        token = secrets.token_urlsafe(32)
        redis.setex(f"pw_reset:{token}", 3600, f"{row['id']}|{tenant_id}")

        base = os.environ.get("GDX_PUBLIC_BASE_URL", "https://gdx.example.com").rstrip("/")
        reset_link = f"{base}/reset-password?token={token}"

        try:
            _send_platform_email(
                str(row["email"]),
                "Password Reset — DispatchApp",
                f"""<div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:2rem;">
                <h2 style="color:#1e293b;">Reset Your Password</h2>
                <p>You requested a password reset. Click the button below to set a new password:</p>
                <p style="text-align:center;margin:1.5rem 0;">
                  <a href="{reset_link}" style="background:#3b82f6;color:white;padding:12px 24px;
                    border-radius:6px;text-decoration:none;font-weight:600;">Reset Password</a>
                </p>
                <p style="color:#64748b;font-size:0.85rem;">This link expires in 1 hour.
                If you didn't request this, ignore this email.</p>
                <p style="color:#94a3b8;font-size:0.75rem;">Link: {reset_link}</p>
                </div>""",
            )
        except Exception:
            log.exception("forgot_password_email_failed")

        try:
            log_audit_event_sync(
                db, tenant_id=tenant_id, user_id=str(row["id"]),
                action="password_reset_requested", entity_type="auth",
                entity_id=str(row["id"]),
                details={"email": email, "token_prefix": token[:8]},
                request=request,
            )
            db.commit()
        except Exception:
            log.exception("forgot_password_audit_error")

    return {"ok": True, "message": "If an account exists with this email, a reset link has been sent."}


@router.post("/reset-password")
def reset_password(body: ResetPasswordBody, request: Request, db: Session = Depends(get_db)):
    """Reset password using a token from the forgot-password email."""
    stored = redis.get(f"pw_reset:{body.token}")
    if not stored:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token.")

    try:
        user_id, tenant_id = str(stored).split("|", 1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid reset token.") from exc

    from werkzeug.security import generate_password_hash
    new_hash = generate_password_hash(body.new_password)

    from gdx_dispatch.models.tenant_models import User as _User
    _u = db.execute(select(_User).where(_User.id == user_id)).scalars().first()
    if not _u:
        raise HTTPException(status_code=400, detail="User not found.")
    _u.password_hash = new_hash

    db.commit()
    redis.delete(f"pw_reset:{body.token}")

    try:
        log_audit_event_sync(
            db, tenant_id=tenant_id, user_id=user_id,
            action="password_reset_success", entity_type="auth",
            entity_id=user_id, details={}, request=request,
        )
        db.commit()
    except Exception:
        log.exception("reset_password_audit_error")

    return {"ok": True, "message": "Password reset successfully. You can now log in."}


# ---------------------------------------------------------------------------
# SS-7 Slice G — admin token revocation (bounded, single-process)
# ---------------------------------------------------------------------------


class RevokeTokenBody(BaseModel):
    # ``jti`` bounds mirror the RFC 7519 guidance of compact opaque IDs; the
    # upper bound stops an admin mistakenly DoS'ing the in-memory map.
    jti: str = Field(min_length=1, max_length=128)
    # ``expires_at`` must be tz-aware UTC per the Denylist contract (mixing
    # naive/tz-aware datetimes would raise TypeError at comparison time).
    expires_at: datetime


class RevokeUserBody(BaseModel):
    # D-S119-revoke-by-user-vs-jti — body for the cascade-revoke endpoint.
    # `user_id` is the JWT `sub` (tenant `users.id` for human users).
    # `reason` is forensic-audit metadata; defaults match the
    # auth_revoke.revoke_user_sessions() default.
    user_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="admin_revoke", max_length=128)


def _db_verify_enabled() -> bool:
    """Sprint Auth & Identity Hardening — Slice 2 rollback gate.

    Default: enabled. Set ``AUTH_DB_VERIFY_ENABLED=0`` to bypass the
    DB lookup (pure JWT-claim trust — pre-Slice-2 behavior). The plan
    flagged this as a 24h-opt-in for prod so a fixture-path miss
    can't take down /api/* without an obvious recovery valve.
    """
    return os.environ.get("AUTH_DB_VERIFY_ENABLED", "1") not in ("0", "false", "False", "")


def _db_verify_user(request: Request, principal_subject: str, actor_kind: str | None) -> dict | None:
    """Sprint Auth & Identity Hardening — Slice 2.

    Look up the tenant ``users`` row by ``principal.subject``. If the row
    is missing / soft-deleted / deactivated, return ``None`` so the caller
    raises 401 + revokes the family. On success, return a small dict with
    the DB-derived ``role`` so the caller can override the JWT-claim role.

    PAT / service-account branch: ``actor_kind == "service_account"`` —
    PATs validate against ``access_tokens`` (not ``users``), so a missing
    row in ``users`` is normal. Skip the lookup, return ``{}`` so the
    caller treats the principal as DB-verified-by-policy.

    Public/health routes that never set ``request.state.tenant`` — return
    ``None`` and let the caller decide. (In practice ``get_current_user``
    is never called on those routes.)
    """
    if not _db_verify_enabled():
        # Rollback valve. Returning empty dict tells the caller "skip the
        # lookup, trust the JWT" — same shape as the SERVICE_ACCOUNT branch.
        return {}

    if str(actor_kind or "").lower() == "service_account":
        return {}

    tenant = getattr(request.state, "tenant", None)
    if not tenant:
        # No tenant context — nothing to verify against. Public/health
        # routes don't reach here in practice.
        return {}

    # Test harnesses and system probes set minimal tenant dicts without db_url.
    # Skip the lookup rather than 401-bombing the test harness (mirrors the old
    # pre-Phase-C behaviour when db_url_enc was absent from the tenant dict).
    db_url = (tenant.get("db_url") if isinstance(tenant, dict) else getattr(tenant, "db_url", None))
    if not db_url:
        return {}

    try:
        import uuid as _uuid_mod
        from gdx_dispatch.core.database import app_engine
        from gdx_dispatch.models.tenant_models import User as _RTUser

        # Convert string subject to uuid.UUID so Uuid(as_uuid=True) columns
        # bind correctly on SQLite (which calls value.hex on the parameter).
        try:
            subject_uuid = _uuid_mod.UUID(str(principal_subject))
        except (ValueError, AttributeError):
            # Non-UUID sub (e.g. legacy test token) → no users row possible.
            return None

        with app_engine.connect() as conn:
            row = conn.execute(
                select(_RTUser.role, _RTUser.deleted_at, _RTUser.active)
                .where(_RTUser.id == subject_uuid)
            ).first()
    except Exception:
        # Conservative on infrastructure failure: signal "no row" so the
        # caller 401s rather than silently trusting the JWT. Operational
        # monitoring catches DB outages elsewhere; we'd rather ship a 401
        # than a privilege-escalation when the lookup can't run.
        log.exception("auth_db_verify_failed sub=%s", principal_subject)
        return None

    if row is None:
        return None
    db_role, deleted_at, active = row[0], row[1], row[2]
    if deleted_at is not None:
        return None
    if active is False:
        return None
    return {"role": str(db_role or "user")}


def _enforce_tenant_match(request: Request, user_tenant_id: str) -> None:
    """Sprint Auth & Identity Hardening — Slice 6 (defense in depth).

    A token minted for tenant A presented on tenant B's host is rejected
    with 403, not silently used. RLS and per-tenant DBs already isolate
    *data*, but this kills the class of "I have valid claims for the
    wrong tenant" scenarios cleanly at the auth boundary.

    No-op if either side is unset (legacy / public routes / health
    probes). Skips PAT / service-account tokens whose tenant_id may
    legitimately be empty.
    """
    if not user_tenant_id:
        return
    host_tenant = getattr(request.state, "tenant", None)
    if not host_tenant:
        return
    host_tenant_id = str(host_tenant.get("id", "") if isinstance(host_tenant, dict) else getattr(host_tenant, "id", ""))
    if not host_tenant_id:
        return
    if str(user_tenant_id) == host_tenant_id:
        return
    log.warning(
        "auth_tenant_mismatch user_tid=%s host_tid=%s path=%s",
        user_tenant_id,
        host_tenant_id,
        getattr(request, "url", None),
    )
    raise HTTPException(
        status_code=403,
        detail="Token tenant does not match request host",
    )


def finalize_login_jwt(
    request: Request,
    *,
    sub: str,
    tenant_claim: str | None,
    role: str,
    actor_kind: str,
    jti: str | None = None,
    imp_actor_id: str | None = None,
    imp_purpose: str | None = None,
) -> dict[str, str]:
    """Run the post-decode auth-identity-hardening gates (Slices 2 + 6 + H).

    Doug 2026-05-10 / D-S118-dispatcher-jwt-gap: the composite-dispatcher
    `_dispatch_login_jwt` runs the same decode this function's two callers
    do (`validate_principal` primary + `jwt.decode` legacy). If those two
    paths diverge from each other on the AFTER-decode gates, every router
    on the dispatcher silently re-opens the bypass class the auth-identity
    hardening sprint just closed. So both callers route through here.

    Gates applied in order:
      Slice H (denylist) — runs FIRST so a revoked token doesn't burn a
        DB lookup. Matches FusionAuth's documented order + flask-jwt-extended.
        Pre-fix (D-S119-legacy-denylist-gap, surfaced 2026-05-10 by prod
        revoke-recipe verification) the denylist check only ran inside
        `validate_principal` — which is unreachable for locally-signed
        login JWTs minted by `_issue()` (no Authentik iss/aud). That made
        `/auth/admin/revoke` silently inert for 100% of prod tokens.
        FAPI 2.0 §5.3.1: "Resource servers SHALL verify the validity,
        integrity, expiration and revocation status of access tokens."
      Slice 2 (DB-verify user) — denies missing/deleted/inactive users,
        overlays the canonical `users.role` over the JWT-claim role so a
        DB-side demote takes effect on the next request.
      Slice 6 (tenant match) — JWT tenant_id must match the host-resolved
        tenant or 403. Defense in depth on top of RLS.
    """
    # Slice H — denylist gate (cheap; runs before Slice 2's DB hit).
    if jti and _get_app_denylist(request).contains(str(jti)):
        log.info("auth_denied_token_revoked sub=%s", sub)
        raise _unauth("Token revoked")

    user_dict = {
        "user_id": str(sub),
        "tenant_id": str(tenant_claim or ""),
        "role": str(role or "user"),
        "imp_actor_id": imp_actor_id,
        "imp_purpose": imp_purpose,
    }
    # Slice 2 — DB verify + role overlay.
    verified = _db_verify_user(request, user_dict["user_id"], actor_kind)
    if verified is None:
        log.info("auth_denied_db_verify sub=%s", user_dict["user_id"])
        raise _unauth("User no longer eligible")
    if "role" in verified:
        user_dict["role"] = verified["role"]
    # Stash on request.state so audit helpers see it without per-route plumbing.
    request.state.user = user_dict
    # Slice 6 — tenant match (raises 403 on mismatch).
    _enforce_tenant_match(request, user_dict["tenant_id"])
    return user_dict


async def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
) -> dict[str, str]:
    # SS-7 Slice F: route the access-token decode through the SS-7 core
    # validator (`gdx_dispatch.core.auth.validate_principal`) as the primary path, with
    # the legacy `jwt.decode` retained as a fallback so HS256 deployments and
    # locally-signed RS256 tokens minted by `_issue()` keep working during the
    # staged rollout.
    #
    # SS-7 Slice H: the denylist is resolved from ``request.app.state`` via
    # :func:`_get_app_denylist` so revoke-writes made through the admin
    # endpoint are observed by the very next auth-read on the same app.
    #
    # Lazy import: `gdx_dispatch.core.auth` re-exports `get_current_user` from this
    # module for legacy callers, so a module-level import would be circular.
    from gdx_dispatch.core.auth import validate_principal
    from gdx_dispatch.core.auth_jwt import JWTValidationError

    public_keys_by_provider: dict[str, bytes | str] = {}
    if ALG == "RS256" and VERIFY_KEY:
        # No network JWKS fetch this slice — reuse the locally-configured
        # RS256 verifying key for both SS-6 providers. A future slice wires
        # Authentik's JWKS resolver.
        public_keys_by_provider = {
            "gdx-spa": VERIFY_KEY,
            "gdx-thirdparty": VERIFY_KEY,
        }

    denylist = _get_app_denylist(request)

    # SS-8 Slice E — first production consumer of the execution-context helper.
    # Wrap the primary-path ``validate_principal`` call in a scoped override
    # that pins ``installation_id=None`` / ``act_chain=()`` for this request.
    # Rationale: the auth dependency is the single boundary where an HTTP
    # request becomes an authenticated principal; binding the Slice B
    # contextvars here guarantees the validator (and anything it calls) sees
    # the asUser-without-installation defaults, and the ``finally`` inside
    # the helper restores the outer context even if validation raises. This
    # slice is intentionally behavior-preserving for existing traffic — a
    # later slice plumbs real signed-installation values through here.
    primary_error: JWTValidationError | None = None
    try:
        with execution_context(installation_id=None, act_chain=()):
            principal = validate_principal(
                token,
                public_keys_by_provider=public_keys_by_provider,
                denylist=denylist,
            )
    except JWTValidationError as exc:
        # Includes TokenRevoked — do not leak internals, do not raise 500.
        # Fall through to the legacy decode: HS256 tokens and locally-signed
        # RS256 tokens (which lack the Authentik iss/aud/gdx_tid shape) are
        # expected to fail the core validator and land on the legacy path.
        primary_error = exc
    else:
        # Phase D cc2-s46: surface impersonation markers (set by the CC
        # impersonate endpoint) so downstream audit hooks can tag operator
        # actions distinctly from real-user actions. Both claims are
        # absent on normal tenant tokens — None means "not impersonation."
        return finalize_login_jwt(
            request,
            sub=principal.subject,
            tenant_claim=principal.tenant_id,
            role=str(principal.raw_claims.get("role", "user")),
            actor_kind=getattr(principal.actor_kind, "value", None)
            or str(principal.actor_kind or ""),
            jti=principal.jti,
            imp_actor_id=principal.raw_claims.get("imp_actor_id"),
            imp_purpose=principal.raw_claims.get("imp_purpose"),
        )

    try:
        c = jwt.decode(token, VERIFY_KEY, algorithms=[ALG])
        if c.get("typ") not in (None, "access"):
            raise _unauth("Invalid access token")
        # Legacy decode path — same gate stack via finalize_login_jwt.
        return finalize_login_jwt(
            request,
            sub=str(c["sub"]),
            tenant_claim=str(c.get("tenant_id", "") or c.get("gdx_tid", "")),
            role=str(c.get("role", "user")),
            actor_kind="human",
            jti=c.get("jti"),
            imp_actor_id=c.get("imp_actor_id"),
            imp_purpose=c.get("imp_purpose"),
        )
    except ExpiredSignatureError as exc:
        # Expired tokens are a normal refresh cycle, not an error. The Vue
        # frontend catches the 401, hits /api/auth/refresh, and retries.
        # Logging as ERROR with a traceback every ~16 min was drowning real
        # errors in log noise and spamming Sentry. TD-023.
        log.info("auth_access_token_expired")
        raise _unauth("Invalid or expired access token") from exc
    except (JWTError, KeyError, TypeError, ValueError, AttributeError) as exc:
        # Same reasoning as ExpiredSignatureError above: invalid tokens (wrong
        # algorithm, malformed, signed with an old secret after a key rotation)
        # are normal client state — the browser gets 401, clears the token, and
        # re-logs-in. Logging as ERROR with a full traceback per request was
        # producing 90+ ERROR entries per day for what's expected 401 behavior.
        # Downgrade to WARN with just the exception class + message.
        if primary_error is not None:
            log.warning(
                "auth_access_token_invalid: core=%s:%s legacy=%s:%s",
                type(primary_error).__name__,
                str(primary_error)[:60],
                type(exc).__name__,
                str(exc)[:60],
            )
        else:
            log.warning("auth_access_token_invalid: %s: %s", type(exc).__name__, str(exc)[:120])
        raise _unauth("Invalid or expired access token") from exc


@router.post(
    "/admin/revoke",
    dependencies=[Depends(require_role("owner", "admin"))],
)
def admin_revoke_token(
    body: RevokeTokenBody,
    request: Request,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Record ``body.jti`` on the app-scoped denylist until ``body.expires_at``.

    Slice H wiring — the denylist is resolved via :func:`_get_app_denylist`
    so the write lands on ``request.app.state.denylist``, the same instance
    :func:`get_current_user` reads on the next request. Cross-worker
    persistence and a distinct revoked-token 401 body are still deferred.

    Slice I — after the denylist write succeeds we emit a ``token_revoked``
    audit event so the forensic trail matches the other auth endpoints
    (login, refresh, logout). The audit call is fail-open: an exception in
    the audit path is logged and swallowed so a downstream audit outage
    cannot convert a successful revoke into a 500. Only the ``jti`` and the
    ISO-8601 ``expires_at`` land in ``details`` — the raw token never does.
    """
    denylist = _get_app_denylist(request)
    denylist.add(body.jti, body.expires_at)
    try:
        tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=current_user.get("user_id", ""),
            action="token_revoked",
            entity_type="auth",
            entity_id=body.jti,
            details={"expires_at": body.expires_at.isoformat()},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("token_revoked_audit_error")
    return {"status": "ok"}


@router.post(
    "/admin/revoke-user",
    dependencies=[Depends(require_role("owner", "admin"))],
)
def admin_revoke_user(
    body: RevokeUserBody,
    request: Request,
    current_user: dict[str, str] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Revoke ALL refresh sessions for the target user. Cascade-revoke
    endpoint that wraps :func:`gdx_dispatch.core.auth_revoke.revoke_user_sessions`.

    Industry pattern (Auth0, Okta): two distinct admin APIs are needed —
    per-jti `revoke-token` (the existing /auth/admin/revoke) and per-user
    `revoke-user-sessions` (this endpoint). The per-jti endpoint is too
    narrow for "this user is compromised, kill everything" since access
    tokens can be re-minted via /auth/refresh — only revoking the refresh
    family terminates the user's session lineage.

    Returns ``{"status": "ok", "sessions_revoked": N}`` where N is the
    count of refresh-family jtis marked as used. Idempotent on unknown
    user_id (returns sessions_revoked=0).

    Audit: emits ``user_sessions_revoked`` with the target_user_id and
    sessions_revoked count. The actor (admin who called) is recorded as
    `user_id` (same shape as `admin_revoke_token`). Audit failure is
    fail-open — a downstream audit outage cannot convert a successful
    revoke into a 500.

    D-S119-revoke-by-user-vs-jti (2026-05-10).
    """
    sessions_revoked = revoke_user_sessions(body.user_id, reason=body.reason)
    try:
        tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=current_user.get("user_id", ""),
            action="user_sessions_revoked",
            entity_type="auth",
            entity_id=body.user_id,
            details={
                "target_user_id": body.user_id,
                "sessions_revoked": sessions_revoked,
                "reason": body.reason,
            },
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("user_sessions_revoked_audit_error")
    return {"status": "ok", "sessions_revoked": sessions_revoked}
