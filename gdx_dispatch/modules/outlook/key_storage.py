"""Sprint Outlook Integration — per-tenant + per-user credential storage.

Two layers of Fernet encryption:

- Tenant level (control plane): TenantSettings.outlook_client_secret_enc holds
  the Entra app's client secret. Set once per tenant when Doug pastes from the
  Azure portal (slice S0 runbook).
- Per-user (tenant plane): OutlookAccount.access_token_enc + refresh_token_enc
  hold each employee's OAuth tokens. Set on the OAuth callback (slice S8),
  refreshed by the token-refresh helper (slice S9).

Like phone_com.key_storage, this does NOT silently fall back to plaintext when
GDX_FERNET_KEY is unset — that's a misconfiguration, not a dev affordance.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import TenantSettings
from gdx_dispatch.modules.outlook.models import OutlookAccount


class OutlookKeyStorageError(RuntimeError):
    """Raised when Outlook credential storage operations cannot proceed safely."""


def _fernet() -> Fernet:
    raw = os.getenv("GDX_FERNET_KEY")
    if not raw:
        raise OutlookKeyStorageError("GDX_FERNET_KEY not configured")
    try:
        return Fernet(raw.encode())
    except Exception as exc:  # noqa: BLE001
        raise OutlookKeyStorageError(
            f"GDX_FERNET_KEY is not a valid Fernet key: {exc}"
        ) from exc


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Tenant-level: client secret on TenantSettings (control plane) ────


def _ensure_tenant_settings(db: Session, tenant_id: UUID) -> TenantSettings:
    settings = db.get(TenantSettings, tenant_id)
    if settings is None:
        settings = TenantSettings()
        settings.tenant_id = tenant_id
        db.add(settings)
    return settings


def set_client_secret(
    db: Session,
    tenant_id: UUID,
    client_secret: str,
    *,
    user_id: str | None = None,
) -> None:
    """Encrypt + store the Entra app client secret. Stamps outlook_secret_set_at."""
    encrypted = _fernet().encrypt(client_secret.encode()).decode()
    settings = _ensure_tenant_settings(db, tenant_id)
    settings.outlook_client_secret_enc = encrypted
    settings.outlook_secret_set_at = _now()


def get_client_secret(db: Session, tenant_id: UUID) -> str | None:
    """Decrypt + return the Entra app client secret, or None if unset.

    Raises OutlookKeyStorageError on Fernet decrypt failure.
    """
    settings = db.get(TenantSettings, tenant_id)
    if settings is None or not settings.outlook_client_secret_enc:
        return None
    try:
        return _fernet().decrypt(settings.outlook_client_secret_enc.encode()).decode()
    except InvalidToken as exc:
        raise OutlookKeyStorageError(
            f"outlook_client_secret_enc decrypt failed for tenant {tenant_id}: {exc}"
        ) from exc


def clear_client_secret(
    db: Session,
    tenant_id: UUID,
    *,
    user_id: str | None = None,
) -> None:
    """Wipe the Entra app client secret."""
    settings = db.get(TenantSettings, tenant_id)
    if settings is not None:
        settings.outlook_client_secret_enc = None
        settings.outlook_secret_set_at = None


# ── Per-user: access + refresh tokens on OutlookAccount (tenant plane) ─


def _find_user_account_most_recent(tenant_db: Session, user_id: UUID) -> OutlookAccount | None:
    """Return the most-recently-connected OutlookAccount for a user, or None.

    Defensive against duplicate (user_id, provider='outlook') rows in tenants
    where the unique index hasn't landed / cleanup hasn't run. Most-recent
    wins; older duplicates are ignored until the cleanup tool removes them.
    """
    return (
        tenant_db.query(OutlookAccount)
        .filter(OutlookAccount.user_id == str(user_id), OutlookAccount.provider == "outlook")
        .order_by(
            OutlookAccount.connected_at.desc().nullslast(),
            OutlookAccount.created_at.desc().nullslast(),
        )
        .first()
    )


def set_user_tokens(
    tenant_db: Session,
    user_id: UUID,
    *,
    access_token: str,
    refresh_token: str,
    access_token_expires_at: datetime,
    upn: str | None = None,
    display_name: str | None = None,
    scopes: str | None = None,
) -> OutlookAccount:
    """Upsert an OutlookAccount row with Fernet-encrypted tokens."""
    account = _find_user_account_most_recent(tenant_db, user_id)
    if account is None:
        account = OutlookAccount()
        account.user_id = str(user_id)
        account.provider = "outlook"
        tenant_db.add(account)

    fernet = _fernet()
    account.access_token_enc = fernet.encrypt(access_token.encode()).decode()
    account.refresh_token_enc = fernet.encrypt(refresh_token.encode()).decode()
    account.access_token_expires_at = access_token_expires_at
    account.connected_at = account.connected_at or _now()
    if upn is not None:
        account.upn = upn
    if display_name is not None:
        account.display_name = display_name
    if scopes is not None:
        account.scopes = scopes
    account.last_error = None
    return account


def get_user_tokens(
    tenant_db: Session,
    user_id: UUID,
) -> tuple[str, str, datetime] | None:
    """Decrypt + return (access_token, refresh_token, access_token_expires_at).

    Returns None if no account exists or tokens are missing. Raises
    OutlookKeyStorageError on decrypt failure.
    """
    account = _find_user_account_most_recent(tenant_db, user_id)
    if account is None or not account.access_token_enc or not account.refresh_token_enc:
        return None
    try:
        fernet = _fernet()
        return (
            fernet.decrypt(account.access_token_enc.encode()).decode(),
            fernet.decrypt(account.refresh_token_enc.encode()).decode(),
            account.access_token_expires_at,
        )
    except InvalidToken as exc:
        raise OutlookKeyStorageError(
            f"token decrypt failed for user {user_id}: {exc}"
        ) from exc


def clear_user_tokens(tenant_db: Session, user_id: UUID) -> None:
    """Disconnect: wipe tokens on every matching row (handles dup-row tenants)
    but keep the account rows + historical messages."""
    accounts = (
        tenant_db.query(OutlookAccount)
        .filter(OutlookAccount.user_id == str(user_id), OutlookAccount.provider == "outlook")
        .all()
    )
    for account in accounts:
        account.access_token_enc = None
        account.refresh_token_enc = None
        account.access_token_expires_at = None
        account.delta_token = None
