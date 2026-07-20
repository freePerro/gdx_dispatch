"""Sprint Outlook Integration — OAuth router (s7 start + s8 callback + s11 account).

Three endpoints under ``/api/oauth/outlook``:

- ``GET /start`` (s7) — 302 redirects an authed user to Microsoft Entra's
  authorize URL with a signed CSRF state. State carries user_id +
  tenant_id; callback validates the signature + matches user_id against
  the authed callback request.
- ``GET /callback`` (s8) — exchanges the auth code for access + refresh
  tokens, hits /me to capture upn/display, persists encrypted tokens via
  ``key_storage.set_user_tokens``, redirects to ``/settings?integration=outlook``.
- ``GET /account`` and ``DELETE /account`` (s11) — connection state +
  idempotent disconnect.

Auth: ``gdx_dispatch.routers.auth.get_current_user`` (SS-7 SPA-compatible JWT path)
— same dep that admin_ai_settings uses for the Tenant AI sprint.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import Tenant, TenantSettings
from gdx_dispatch.core.database import get_db, get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.outlook import key_storage
from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError, OutlookGraphClient
from gdx_dispatch.modules.outlook.models import OutlookAccount
from gdx_dispatch.routers.auth import get_current_user


log = logging.getLogger("gdx_dispatch.routers.outlook_oauth")

router = APIRouter(
    prefix="/api/oauth/outlook",
    tags=["oauth", "outlook"],
)

OAUTH_SCOPES = (
    "User.Read Mail.Read Mail.ReadWrite Mail.Send "
    "MailboxSettings.Read offline_access"
)
STATE_MAX_AGE_S = 600  # 10 minutes — Microsoft consent flow within window


# ── shared helpers ─────────────────────────────────────────────────────


class OAuthExchangeError(RuntimeError):
    """Token exchange against Microsoft failed."""


def _state_signer() -> URLSafeTimedSerializer:
    """Sign OAuth state with a server-side secret (≥32 bytes).

    Tries, in order:
      1. STATE_SIGNING_KEY — dedicated env var for OAuth state signing.
      2. JWT_SECRET — used when the app runs HS256-mode JWTs.
      3. SECRET_KEY — the Starlette/FastAPI session signing key, always
         set (compose default ≥32 bytes). This is the fallback for the
         production RS256 path where JWT_SECRET is intentionally unset.

    Production (2026-04-28) uses RS256 with RS_PRIVATE_KEY set and
    JWT_SECRET unset; without SECRET_KEY fallback, /start raised 500
    "signing key not configured". Adding SECRET_KEY as a fallback keeps
    every prod env working out of the box.
    """
    for env_name in ("STATE_SIGNING_KEY", "JWT_SECRET", "SECRET_KEY"):
        secret = os.getenv(env_name)
        if secret and len(secret) >= 32:
            return URLSafeTimedSerializer(secret, salt="outlook-oauth-state")
    raise HTTPException(
        status_code=500,
        detail=(
            "OAuth state signing key not configured. Set STATE_SIGNING_KEY, "
            "JWT_SECRET, or SECRET_KEY (≥32 bytes) in the app environment."
        ),
    )


def get_user_for_oauth_start(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Wrapper so tests can override the auth dep."""
    return user


def get_db_for_oauth_start(db: Session = Depends(get_db)) -> Session:
    """Wrapper so tests can override the control-plane Session dep."""
    return db


def get_db_for_oauth_callback(
    db: Session = Depends(get_db),
) -> Session:
    """Wrapper so tests can override the tenant-plane Session dep."""
    return db


def _build_redirect_uri(tenant_slug: str) -> str:
    base = os.environ.get("TENANT_BASE_DOMAIN", "example.com").strip("/")
    return f"https://{tenant_slug}.{base}/api/oauth/outlook/callback"


def _build_authorize_url(
    *,
    microsoft_tenant_id: str,
    client_id: str,
    redirect_uri: str,
    state: str,
) -> str:
    """Microsoft v2 authorize endpoint."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": OAUTH_SCOPES,
        "state": state,
        "prompt": "select_account",
    }
    return (
        f"https://login.microsoftonline.com/{microsoft_tenant_id}/oauth2/v2.0/authorize"
        f"?{urlencode(params)}"
    )


def _exchange_code_for_tokens(
    *,
    microsoft_tenant_id: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict:
    """Form-encoded POST to Microsoft v2 token endpoint."""
    url = (
        f"https://login.microsoftonline.com/{microsoft_tenant_id}"
        f"/oauth2/v2.0/token"
    )
    form = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "scope": OAUTH_SCOPES,
    }
    with httpx.Client(timeout=30) as c:
        resp = c.post(
            url,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = resp.text
        raise OAuthExchangeError(f"token exchange {resp.status_code}: {body}")
    return resp.json()


def _safe_redirect(*, status_q: str, detail: str | None = None) -> RedirectResponse:
    """Always redirect to a same-origin Settings URL — never echo external input."""
    # urlencode the params so a crafted status/detail can't inject query
    # params or break out of the same-origin path. (CodeQL url-redirection)
    params = {"integration": "outlook", "status": status_q}
    if detail:
        params["detail"] = detail
    return RedirectResponse(
        f"/settings?{urlencode(params)}",
        status_code=302,
    )


# ── /start (s7) ────────────────────────────────────────────────────────


class OutlookOAuthStartOut(BaseModel):
    authorize_url: str


@router.post(
    "/start",
    response_model=OutlookOAuthStartOut,
    dependencies=[Depends(require_module("email"))],
)
def start_oauth(
    user: dict[str, Any] = Depends(get_user_for_oauth_start),
    db: Session = Depends(get_db_for_oauth_start),
) -> OutlookOAuthStartOut:
    """Mint a Microsoft consent URL for the authed user.

    Browsers can't carry the Authorization: Bearer header on a top-level
    `window.location.href` navigation, so an SPA can't `GET /start` directly.
    This endpoint accepts an authed POST (Bearer header), validates the
    user + tenant configuration, signs a CSRF state, and returns the
    Microsoft authorize URL. The SPA then sets `window.location.href` to
    the returned URL — that's a navigation away from our origin to MS, so
    no header is needed.

    The /callback continuation handles itself — Microsoft redirects to it
    with the auth code in the query string and our signed state intact.
    """
    user_id = user.get("user_id") or user.get("id") or user.get("sub")
    tenant_id_raw = user.get("tenant_id")
    if not user_id or not tenant_id_raw:
        raise HTTPException(status_code=400, detail="missing user/tenant context")
    tenant_id = tenant_id_raw if isinstance(tenant_id_raw, UUID) else UUID(str(tenant_id_raw))

    settings = db.get(TenantSettings, tenant_id)
    if (
        settings is None
        or not settings.outlook_client_id
        or not settings.outlook_microsoft_tenant_id
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Outlook not configured for this tenant. "
                "An admin must paste the Entra app credentials in "
                "Settings → Integrations → Outlook before users can connect."
            ),
        )

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    # Per-call nonce makes each state cryptographically unique even for back-
    # to-back clicks; mitigates state-replay attacks beyond the 10-min max-age.
    import secrets
    state_payload = {
        "user_id": str(user_id),
        "tenant_id": str(tenant_id),
        "nonce": secrets.token_urlsafe(16),
    }
    state = _state_signer().dumps(state_payload)
    redirect_uri = _build_redirect_uri(tenant.slug)
    authorize_url = _build_authorize_url(
        microsoft_tenant_id=settings.outlook_microsoft_tenant_id,
        client_id=settings.outlook_client_id,
        redirect_uri=redirect_uri,
        state=state,
    )
    log.info("outlook oauth start: user=%s tenant=%s", user_id, tenant_id)
    return OutlookOAuthStartOut(authorize_url=authorize_url)


# ── /callback (s8) ─────────────────────────────────────────────────────


@router.get(
    "/callback",
    dependencies=[Depends(require_module("email"))],
)
def oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    control_db: Session = Depends(get_db_for_oauth_start),
    tenant_db: Session = Depends(get_db_for_oauth_callback),
) -> RedirectResponse:
    """Microsoft redirects the user's browser here after consent.

    This is a top-level browser navigation from microsoftonline.com, so
    we cannot rely on Authorization: Bearer headers — the SPA's token is
    in sessionStorage, which doesn't accompany a cross-origin redirect.
    The user's identity is carried in the *signed state* (server-signed
    with SECRET_KEY at /start time, ≥32 bytes, 10-min max-age, per-call
    nonce). The state signature IS the authentication proof; an attacker
    cannot forge a state without our signing key.
    """
    if error:
        log.warning("outlook oauth callback error from microsoft: %s — %s", error, error_description)
        return _safe_redirect(status_q="error", detail=error)
    if not code or not state:
        return _safe_redirect(status_q="error", detail="missing_code_or_state")

    try:
        payload = _state_signer().loads(state, max_age=STATE_MAX_AGE_S)
    except Exception:  # noqa: BLE001
        log.warning("outlook oauth callback: state signature/age invalid (could be expired or tampered)", exc_info=True)
        return _safe_redirect(status_q="error", detail="invalid_state")

    state_user_id = str(payload.get("user_id") or "")
    state_tenant_id = str(payload.get("tenant_id") or "")
    if not state_user_id or not state_tenant_id:
        return _safe_redirect(status_q="error", detail="state_missing_ids")

    tenant_id = UUID(state_tenant_id)
    user_id = UUID(state_user_id)

    settings = control_db.get(TenantSettings, tenant_id)
    if settings is None or not settings.outlook_client_id:
        return _safe_redirect(status_q="error", detail="tenant_not_configured")

    client_secret = key_storage.get_client_secret(control_db, tenant_id)
    if not client_secret:
        return _safe_redirect(status_q="error", detail="client_secret_missing")

    tenant = control_db.get(Tenant, tenant_id)
    if tenant is None or not tenant.slug:
        return _safe_redirect(status_q="error", detail="tenant_not_found")
    redirect_uri = _build_redirect_uri(tenant.slug)

    try:
        tok = _exchange_code_for_tokens(
            microsoft_tenant_id=settings.outlook_microsoft_tenant_id,
            client_id=settings.outlook_client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
    except OAuthExchangeError as exc:
        log.warning("outlook token exchange failed for user %s: %s", user_id, exc)
        return _safe_redirect(status_q="error", detail="token_exchange_failed")

    expires_in = int(tok.get("expires_in", 3600))
    access_token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    # Identity check via /me — best-effort (tokens are still valid even if /me fails)
    access_token = tok.get("access_token")
    refresh_token = tok.get("refresh_token")
    if not access_token:
        log.warning("outlook callback: token response missing access_token for user %s", user_id)
        return _safe_redirect(status_q="error", detail="no_access_token")
    if not refresh_token:
        # Microsoft only returns a refresh token if `offline_access` was
        # granted. If the user denied that scope on consent, we get an
        # access token only — useless for our long-running mailbox sync.
        log.warning("outlook callback: token response missing refresh_token (offline_access denied?) for user %s", user_id)
        return _safe_redirect(status_q="error", detail="offline_access_required")

    upn = None
    display_name = None
    try:
        with OutlookGraphClient(access_token) as gc:
            ident = gc.validate_token()
        upn = ident.upn
        display_name = ident.display_name
    except OutlookGraphAPIError as exc:
        log.warning("outlook /me failed for user %s: %s", user_id, exc)

    account = key_storage.set_user_tokens(
        tenant_db, user_id,
        access_token=access_token,
        refresh_token=refresh_token,
        access_token_expires_at=access_token_expires_at,
        upn=upn,
        display_name=display_name,
        scopes=tok.get("scope"),
    )
    # Snapshot "was this account ever synced?" BEFORE commit expires the
    # instance — this is a real in-session read (None for a brand-new row).
    is_fresh_connect = account.last_sync_at is None
    tenant_db.commit()
    # account.id is assigned by SQLAlchemy at FLUSH (mapped_column default=uuid4),
    # NOT at construction — so it must be read AFTER commit. Reading it before
    # would enqueue backfill with "None" and crash the worker on UUID("None").
    account_id = str(account.id)

    # Best-effort Graph webhook subscription so real-time notifications
    # start immediately. 2026-07-07 audit: despite create_subscription's
    # "on connect" docstring, nothing ever called it — prod ran with an
    # empty outlook_subscriptions table and the 30-minute fallback poll
    # carried all traffic. Failure is non-fatal: renew_all_outlook_
    # subscriptions self-heals missing subscriptions every 6h, and the
    # fallback poll keeps the mailbox synced meanwhile.
    try:
        from gdx_dispatch.modules.outlook.subscriptions import create_subscription

        create_subscription(
            control_db=control_db,
            tenant_db=tenant_db,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        tenant_db.commit()
    except Exception:
        tenant_db.rollback()
        log.exception("outlook callback: subscription create failed (fallback poll still covers sync)")

    # D5: pull mail on connect. REQUIRED, not just nice — create_subscription
    # above just made a HEALTHY subscription, and the fallback poller
    # explicitly skips healthy-subscription accounts, so without this a
    # freshly-connected mailbox stays EMPTY until the first new mail webhook
    # fires. Fresh connect → date-bounded backfill (honors backfill_days and
    # primes the delta tokens the webhook/poller resume from). Reconnect →
    # an immediate delta sync to catch mail missed while disconnected. Both
    # are enqueued (non-blocking); failure is non-fatal (the 6h self-heal +
    # manual sync still catch up).
    try:
        from gdx_dispatch.modules.outlook.models import OutlookSettings
        from gdx_dispatch.modules.outlook.tasks import (
            backfill_outlook_mailbox,
            sync_outlook_mailbox,
        )

        if is_fresh_connect:
            settings_row = (
                tenant_db.query(OutlookSettings).filter(OutlookSettings.id == 1).first()
            )
            days = (settings_row.backfill_days if settings_row else None) or 90
            backfill_outlook_mailbox.delay(account_id, str(tenant_id), days=days)
        else:
            sync_outlook_mailbox.delay(account_id, str(tenant_id))
    except Exception:
        log.exception("outlook callback: initial sync enqueue failed (poll/manual catches up)")

    return _safe_redirect(status_q="ok")


# ── /account GET + DELETE (s11) ────────────────────────────────────────


class OutlookAccountState(BaseModel):
    connected: bool
    upn: str | None = None
    display_name: str | None = None
    connected_at: str | None = None
    last_sync_at: str | None = None
    last_error: str | None = None


@router.get(
    "/account",
    response_model=OutlookAccountState,
    dependencies=[Depends(require_module("email"))],
)
def get_account(
    user: dict[str, Any] = Depends(get_user_for_oauth_start),
    tenant_db: Session = Depends(get_db_for_oauth_callback),
) -> OutlookAccountState:
    user_id_raw = user.get("user_id") or user.get("id") or user.get("sub")
    if not user_id_raw:
        raise HTTPException(status_code=400, detail="missing user context")
    user_id = user_id_raw if isinstance(user_id_raw, UUID) else UUID(str(user_id_raw))

    account = (
        tenant_db.query(OutlookAccount)
        .filter(OutlookAccount.user_id == str(user_id), OutlookAccount.provider == "outlook")
        .one_or_none()
    )
    if account is None or not account.access_token_enc:
        return OutlookAccountState(connected=False)
    return OutlookAccountState(
        connected=True,
        upn=account.upn,
        display_name=account.display_name,
        connected_at=account.connected_at.isoformat() if account.connected_at else None,
        last_sync_at=account.last_sync_at.isoformat() if account.last_sync_at else None,
        last_error=account.last_error,
    )


@router.delete(
    "/account",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_module("email"))],
)
def disconnect_account(
    user: dict[str, Any] = Depends(get_user_for_oauth_start),
    tenant_db: Session = Depends(get_db_for_oauth_callback),
) -> None:
    """Idempotent disconnect: clears tokens + delta state. Historical messages persist."""
    user_id_raw = user.get("user_id") or user.get("id") or user.get("sub")
    if not user_id_raw:
        raise HTTPException(status_code=400, detail="missing user context")
    user_id = user_id_raw if isinstance(user_id_raw, UUID) else UUID(str(user_id_raw))

    key_storage.clear_user_tokens(tenant_db, user_id)
    tenant_db.commit()
    log.info("outlook disconnect: user=%s", user_id)
    return None
