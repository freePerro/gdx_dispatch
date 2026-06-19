"""Sprint 1.x — phone_com per-tenant credential storage.

Mirrors ``gdx_dispatch/core/llm/key_storage.py``: Fernet-encrypts the Phone.com
permanent (or OAuth) access token against ``GDX_FERNET_KEY``, persists
into ``tenant_settings.phone_com_token_enc`` (control plane).

Also stores the per-tenant webhook HMAC secret
(``phone_com_webhook_secret``). The secret is generated locally — Phone.com
doesn't issue it. We register a webhook endpoint and tell Phone.com what
secret to sign with; verification on inbound requests reads the same
secret back. Both rotate together on token rotation.

Like the LLM module, this does NOT silently fall back to plaintext when
``GDX_FERNET_KEY`` is unset — that's a misconfiguration, not a dev
affordance.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import Tenant, TenantSettings
from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import SessionLocal as _SessionLocal
from gdx_dispatch.modules.phone_com.client import PhoneComClient


class PhoneComKeyStorageError(RuntimeError):
    """Raised when Phone.com credential storage operations cannot proceed safely."""


def _fernet() -> Fernet:
    raw = os.getenv("GDX_FERNET_KEY")
    if not raw:
        raise PhoneComKeyStorageError("GDX_FERNET_KEY not configured")
    try:
        return Fernet(raw.encode())
    except Exception as exc:  # noqa: BLE001 — surface as typed
        raise PhoneComKeyStorageError(f"GDX_FERNET_KEY is not a valid Fernet key: {exc}") from exc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_settings(db: Session, tenant_id: UUID) -> TenantSettings:
    settings = db.get(TenantSettings, tenant_id)
    if settings is None:
        settings = TenantSettings(tenant_id=tenant_id)
        db.add(settings)
    return settings


def set_token(
    db: Session,
    tenant_id: UUID,
    token: str,
    *,
    user_id: str | None = None,
) -> None:
    """Encrypt + store the Phone.com access token. Upserts the row.

    Clears ``phone_com_token_last_validated_at`` and
    ``phone_com_token_last_error`` — a freshly-set token has not yet been
    test-fired (``client.test_token`` is the validator).
    """
    # Strip leading/trailing whitespace + zero-width chars — copy-paste from
    # the Phone.com console regularly drags in a trailing newline/space which
    # makes Phone.com reject the Bearer header with 401 even though the
    # underlying token is correct.
    cleaned = token.strip().strip("​‌‍﻿")
    if not cleaned:
        raise PhoneComKeyStorageError("token is empty after whitespace strip")
    encrypted = _fernet().encrypt(cleaned.encode()).decode()
    settings = _ensure_settings(db, tenant_id)
    settings.phone_com_token_enc = encrypted
    settings.phone_com_token_set_at = _now()
    settings.phone_com_token_last_validated_at = None
    settings.phone_com_token_last_error = None

    log_audit_event_sync(
        db,
        tenant_id=str(tenant_id),
        user_id=user_id,
        action="phone_com.token_set",
        entity_type="tenant_settings",
        entity_id=str(tenant_id),
        details={},
    )
    db.commit()


def get_token(db: Session, tenant_id: UUID) -> str | None:
    """Return the decrypted token, or ``None`` when no row exists / column is NULL.

    Raises ``PhoneComKeyStorageError`` when ciphertext is present but
    Fernet cannot decrypt it. Silent-None on decryption failure would
    hide a config-rotation incident behind a no-token response.
    """
    settings = db.get(TenantSettings, tenant_id)
    if settings is None or settings.phone_com_token_enc is None:
        return None
    try:
        # Defense-in-depth: strip whitespace on read too in case a token was
        # stored before the set_token strip landed (the "I pasted the right
        # token but Phone.com 401s" class of bug).
        return _fernet().decrypt(settings.phone_com_token_enc.encode()).decode().strip()
    except InvalidToken as exc:
        raise PhoneComKeyStorageError(
            f"tenant {tenant_id} phone_com token cannot be decrypted with current GDX_FERNET_KEY"
        ) from exc


def clear_token(
    db: Session,
    tenant_id: UUID,
    *,
    user_id: str | None = None,
) -> None:
    """Clear all four token-tracking columns. Audit-logged.

    Does NOT clear ``phone_com_webhook_secret`` — the secret survives
    token rotation. Use ``clear_webhook_secret`` separately when the
    webhook is also being deregistered.
    """
    settings = db.get(TenantSettings, tenant_id)
    if settings is not None:
        settings.phone_com_token_enc = None
        settings.phone_com_token_set_at = None
        settings.phone_com_token_last_validated_at = None
        settings.phone_com_token_last_error = None

    log_audit_event_sync(
        db,
        tenant_id=str(tenant_id),
        user_id=user_id,
        action="phone_com.token_cleared",
        entity_type="tenant_settings",
        entity_id=str(tenant_id),
        details={},
    )
    db.commit()


def mark_validated(db: Session, tenant_id: UUID) -> None:
    """Bump ``last_validated_at`` and clear ``last_error``. Called by ``client.test_token`` on success."""
    settings = db.get(TenantSettings, tenant_id)
    if settings is not None:
        settings.phone_com_token_last_validated_at = _now()
        settings.phone_com_token_last_error = None
        db.commit()


def mark_failed(db: Session, tenant_id: UUID, error: str) -> None:
    """Record a validation error. Preserves the prior ``last_validated_at``."""
    settings = db.get(TenantSettings, tenant_id)
    if settings is not None:
        settings.phone_com_token_last_error = error[:500]
        db.commit()


def get_or_create_webhook_secret(db: Session, tenant_id: UUID) -> str:
    """Return the per-tenant HMAC secret used to verify Phone.com webhook payloads.

    Generates a fresh 32-byte URL-safe secret on first call, stored
    Fernet-encrypted in ``phone_com_webhook_secret``. Idempotent on
    subsequent calls.
    """
    settings = db.get(TenantSettings, tenant_id)
    if settings is not None and settings.phone_com_webhook_secret:
        try:
            return _fernet().decrypt(settings.phone_com_webhook_secret.encode()).decode()
        except InvalidToken as exc:
            raise PhoneComKeyStorageError(
                f"tenant {tenant_id} phone_com webhook secret cannot be decrypted"
            ) from exc

    fresh = secrets.token_urlsafe(32)
    encrypted = _fernet().encrypt(fresh.encode()).decode()
    settings = _ensure_settings(db, tenant_id)
    settings.phone_com_webhook_secret = encrypted
    db.commit()
    return fresh


def clear_webhook_secret(db: Session, tenant_id: UUID) -> None:
    settings = db.get(TenantSettings, tenant_id)
    if settings is not None:
        settings.phone_com_webhook_secret = None
        settings.phone_com_webhook_secret_prev = None
        settings.phone_com_webhook_secret_prev_until = None
        db.commit()


def rotate_webhook_secret(
    db: Session, tenant_id: UUID, *, grace_seconds: int = 3600,
) -> tuple[str, str]:
    """Generate a fresh webhook secret. Returns ``(old_plain, new_plain)``.

    Caller is responsible for PATCHing the Phone.com callback URL to point
    at the new secret BEFORE committing the rotation. We commit only after
    the upstream PATCH succeeds — that's the rotator task's job.

    Stages the new secret in ``phone_com_webhook_secret`` and copies the
    OLD secret into ``phone_com_webhook_secret_prev`` with a grace window
    of ``grace_seconds`` (default 1h) so in-flight Phone.com retries
    against the old URL still authenticate.
    """
    from datetime import timedelta

    settings = _ensure_settings(db, tenant_id)
    old_plain = ""
    if settings.phone_com_webhook_secret:
        try:
            old_plain = _fernet().decrypt(settings.phone_com_webhook_secret.encode()).decode()
        except InvalidToken as exc:
            raise PhoneComKeyStorageError(
                f"tenant {tenant_id} cannot decrypt current webhook secret to rotate"
            ) from exc
    new_plain = secrets.token_urlsafe(32)
    settings.phone_com_webhook_secret_prev = settings.phone_com_webhook_secret
    settings.phone_com_webhook_secret_prev_until = (
        _now() + timedelta(seconds=grace_seconds) if old_plain else None
    )
    settings.phone_com_webhook_secret = _fernet().encrypt(new_plain.encode()).decode()
    settings.phone_com_webhook_rotated_at = _now()
    db.commit()
    return old_plain, new_plain


def revert_webhook_secret(db: Session, tenant_id: UUID) -> None:
    """Roll back a rotation if the upstream PATCH failed. Restores
    ``phone_com_webhook_secret`` from ``_prev`` and clears the grace fields.
    """
    settings = db.get(TenantSettings, tenant_id)
    if settings is None or settings.phone_com_webhook_secret_prev is None:
        return
    settings.phone_com_webhook_secret = settings.phone_com_webhook_secret_prev
    settings.phone_com_webhook_secret_prev = None
    settings.phone_com_webhook_secret_prev_until = None
    db.commit()


def test_and_cache_account(control_db: Session, tenant_id: UUID) -> dict[str, Any]:
    """Validate token, cache account features in AppSettings, and mark storage status.

    Returns a dict with 'ok', 'features_cached', and optional 'error' or 'voip_id'.
    """
    token = get_token(control_db, tenant_id)
    if token is None:
        return {"ok": False, "error": "no token set", "features_cached": False}

    client = PhoneComClient(token=token)
    result = client.test_token()

    if result["ok"]:
        from gdx_dispatch.models.tenant_models import AppSettings

        with _SessionLocal() as tenant_db:
            settings = tenant_db.query(AppSettings).first()
            if settings is None:
                settings = AppSettings()
                tenant_db.add(settings)
                tenant_db.commit()

            settings.phone_com_account_features = result.get("features")
            if settings.phone_com_voip_id is None and result.get("voip_id"):
                settings.phone_com_voip_id = str(result["voip_id"])
            tenant_db.commit()

        mark_validated(control_db, tenant_id)
        return result | {"features_cached": True}
    else:
        mark_failed(control_db, tenant_id, error=result["error"] or "unknown")
        return result | {"features_cached": False}
