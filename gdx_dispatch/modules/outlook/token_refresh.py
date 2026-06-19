"""Sprint Outlook Integration — refresh-token grant + 401-retry wrapper.

Microsoft access tokens last 1 hour. Refresh tokens last ~90 days
(rolling: each refresh extends the window).

Callers should use `with_outlook_client(control_db, tenant_db, user_id)` —
the context manager handles proactive refresh + 401-retry transparently.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import TenantSettings
from gdx_dispatch.modules.outlook.models import OutlookAccount
from gdx_dispatch.modules.outlook import key_storage
from gdx_dispatch.modules.outlook.graph_client import OutlookGraphAPIError, OutlookGraphClient


log = logging.getLogger("gdx_dispatch.modules.outlook.token_refresh")


REFRESH_THRESHOLD_S = 300  # 5 minutes
OAUTH_SCOPES = (
    "User.Read Mail.Read Mail.ReadWrite Mail.Send "
    "MailboxSettings.Read offline_access"
)


class OutlookReconnectRequired(RuntimeError):
    """Refresh failed — user must re-OAuth via /api/oauth/outlook/start."""


class OutlookTransientRetry(RuntimeError):
    """Token was successfully refreshed mid-call; caller should retry the
    Graph operation ONCE with the new access token. Distinct from
    OutlookReconnectRequired (which is terminal — user must reconnect)."""


def _post_refresh(
    *,
    microsoft_tenant_id: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict:
    url = (
        f"https://login.microsoftonline.com/{microsoft_tenant_id}"
        f"/oauth2/v2.0/token"
    )
    form = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "scope": OAUTH_SCOPES,
    }
    with httpx.Client(timeout=30) as c:
        resp = c.post(
            url, data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = resp.text
        raise OutlookReconnectRequired(f"refresh failed {resp.status_code}: {body}")
    return resp.json()


def refresh_user_tokens(
    control_db: Session,
    tenant_db: Session,
    user_id: UUID,
) -> str:
    """Refresh tokens for this user. Returns the new access_token.

    Raises OutlookReconnectRequired if no refresh token, or if Microsoft
    rejects the refresh (e.g., user revoked consent in account.microsoft.com).
    """
    account = (
        tenant_db.query(OutlookAccount)
        .filter(OutlookAccount.user_id == str(user_id), OutlookAccount.provider == "outlook")
        .one_or_none()
    )
    if account is None or not account.refresh_token_enc:
        raise OutlookReconnectRequired("no refresh token on file")

    tokens = key_storage.get_user_tokens(tenant_db, user_id)
    if tokens is None:
        raise OutlookReconnectRequired("token decrypt failed")
    _, refresh_token, _ = tokens

    # Look up tenant from the OutlookAccount's connection — the tenant_db is
    # already scoped to this tenant by connection isolation. The control-plane
    # settings live in control_db; we need the tenant_id to look them up. We
    # carry it via the user_id: every user belongs to exactly one tenant; the
    # caller must already know the tenant_id (passes control_db scoped to it).
    # To avoid an extra arg, accept the tenant_id as a session attribute the
    # caller sets via control_db.info["tenant_id"].
    tenant_id = control_db.info.get("tenant_id") if hasattr(control_db, "info") else None
    if tenant_id is None:
        # Fallback: read AppSettings from tenant_db (every tenant has one row).
        # AppSettings doesn't carry tenant_id (three-plane), so we instead infer
        # via key_storage helpers that need it. The cleanest contract: callers
        # pass tenant_id explicitly via control_db.info — set via the
        # `with_outlook_client` wrapper below.
        raise OutlookReconnectRequired("control_db missing tenant_id context")
    settings = control_db.get(TenantSettings, tenant_id)
    if settings is None or not settings.outlook_client_id:
        raise OutlookReconnectRequired("tenant outlook config missing")
    client_secret = key_storage.get_client_secret(control_db, tenant_id)
    if not client_secret:
        raise OutlookReconnectRequired("client secret missing")

    try:
        tok = _post_refresh(
            microsoft_tenant_id=settings.outlook_microsoft_tenant_id,
            client_id=settings.outlook_client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
        )
    except OutlookReconnectRequired as exc:
        account.last_error = str(exc)[:500]
        tenant_db.commit()
        raise

    expires_in = int(tok.get("expires_in", 3600))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    key_storage.set_user_tokens(
        tenant_db, user_id,
        access_token=tok["access_token"],
        # Microsoft rotates the refresh token on each refresh — use the new one if present
        refresh_token=tok.get("refresh_token", refresh_token),
        access_token_expires_at=expires_at,
        scopes=tok.get("scope"),
    )
    tenant_db.commit()
    return tok["access_token"]


@contextmanager
def with_outlook_client(
    control_db: Session,
    tenant_db: Session,
    user_id: UUID,
    tenant_id: UUID,
) -> Iterator[OutlookGraphClient]:
    """Yield a Graph client with valid token. Refreshes proactively + on 401.

    Usage:
        with with_outlook_client(control_db, tenant_db, user_id, tenant_id) as gc:
            messages = gc.list_messages()
    """
    # Stash tenant_id on control_db.info so refresh_user_tokens can find it
    if hasattr(control_db, "info"):
        control_db.info["tenant_id"] = tenant_id

    tokens = key_storage.get_user_tokens(tenant_db, user_id)
    if tokens is None:
        raise OutlookReconnectRequired("user has not connected Outlook")
    access_token, _, expires_at = tokens

    if expires_at is not None and (
        expires_at - datetime.now(timezone.utc)
    ).total_seconds() < REFRESH_THRESHOLD_S:
        access_token = refresh_user_tokens(control_db, tenant_db, user_id)

    client = OutlookGraphClient(access_token)
    try:
        yield client
    except OutlookGraphAPIError as exc:
        if exc.status_code != 401:
            raise
        # 401 mid-call: the access token JUST expired. Refresh succeeded ⇒
        # raise OutlookTransientRetry so the caller retries ONCE. If the
        # refresh itself fails, refresh_user_tokens raises
        # OutlookReconnectRequired (terminal — user must reconnect).
        client.close()
        try:
            refresh_user_tokens(control_db, tenant_db, user_id)
        except OutlookReconnectRequired:
            raise  # terminal — caller must surface "reconnect" to user
        raise OutlookTransientRetry(
            "access_token expired mid-call; tokens refreshed — retry once"
        ) from exc
    finally:
        client.close()
