"""Per-tenant LLM provider API key storage.

Sprint 1.x-S4 (set/get/clear) + S6 (test_the_key). Fernet-encrypts the key
against ``GDX_FERNET_KEY`` (same env var ``gdx_dispatch/core/database.py:_decrypt_db_url``
uses for tenant DB URLs); reads and writes go through ``tenant_settings``
(control plane, landed in S3).

Unlike the tenant-DB-URL path, this module does NOT silently fall back to
plaintext when the Fernet key is unset — a missing ``GDX_FERNET_KEY`` is a
misconfiguration, not a dev affordance.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import TenantSettings
from gdx_dispatch.core.audit import log_audit_event_sync


class LLMKeyStorageError(RuntimeError):
    """Raised when LLM key storage operations cannot proceed safely."""


def _fernet() -> Fernet:
    raw = os.getenv("GDX_FERNET_KEY")
    if not raw:
        raise LLMKeyStorageError("GDX_FERNET_KEY not configured")
    try:
        return Fernet(raw.encode())
    except Exception as exc:  # noqa: BLE001 — surface as typed
        raise LLMKeyStorageError(f"GDX_FERNET_KEY is not a valid Fernet key: {exc}") from exc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def set_key(
    db: Session,
    tenant_id: UUID,
    key: str,
    *,
    user_id: str | None = None,
) -> None:
    """Encrypt + store ``key`` for ``tenant_id``. Upserts the row.

    Clears ``llm_provider_key_last_validated_at`` and
    ``llm_provider_key_last_error`` — a freshly-set key has not yet been
    test-fired (S6 ``test_the_key`` is the validator)."""
    encrypted = _fernet().encrypt(key.encode()).decode()
    now = _now()

    settings = db.get(TenantSettings, tenant_id)
    if settings is None:
        settings = TenantSettings(
            tenant_id=tenant_id,
            llm_provider_key_enc=encrypted,
            llm_provider_key_set_at=now,
            llm_provider_key_last_validated_at=None,
            llm_provider_key_last_error=None,
        )
        db.add(settings)
    else:
        settings.llm_provider_key_enc = encrypted
        settings.llm_provider_key_set_at = now
        settings.llm_provider_key_last_validated_at = None
        settings.llm_provider_key_last_error = None

    log_audit_event_sync(
        db,
        tenant_id=str(tenant_id),
        user_id=user_id,
        action="ai_settings.key_set",
        entity_type="tenant_settings",
        entity_id=str(tenant_id),
        details={},
    )
    db.commit()


def get_key(db: Session, tenant_id: UUID) -> str | None:
    """Return the decrypted key, or None when no row exists / column is NULL.

    Raises ``LLMKeyStorageError`` when a ciphertext is present but Fernet
    cannot decrypt it. Silent-None on decryption failure would hide a
    config-rotation incident behind a no-key response."""
    settings = db.get(TenantSettings, tenant_id)
    if settings is None or settings.llm_provider_key_enc is None:
        return None
    try:
        return _fernet().decrypt(settings.llm_provider_key_enc.encode()).decode()
    except InvalidToken as exc:
        raise LLMKeyStorageError(
            f"tenant {tenant_id} llm key cannot be decrypted with current GDX_FERNET_KEY"
        ) from exc


def clear_key(
    db: Session,
    tenant_id: UUID,
    *,
    user_id: str | None = None,
) -> None:
    """Clear the key + all four key-tracking columns. Audit-logged.

    No-op (still audit-logged) when no row exists for ``tenant_id``."""
    settings = db.get(TenantSettings, tenant_id)
    if settings is not None:
        settings.llm_provider_key_enc = None
        settings.llm_provider_key_set_at = None
        settings.llm_provider_key_last_validated_at = None
        settings.llm_provider_key_last_error = None

    log_audit_event_sync(
        db,
        tenant_id=str(tenant_id),
        user_id=user_id,
        action="ai_settings.key_cleared",
        entity_type="tenant_settings",
        entity_id=str(tenant_id),
        details={},
    )
    db.commit()


def test_the_key(db: Session, tenant_id: UUID) -> dict[str, Any]:
    """Fire a 1-token Anthropic ``messages.create`` against Haiku 4.5.

    Returns ``{"ok", "model", "latency_ms", "error"}``. Updates
    ``llm_provider_key_last_validated_at`` on success and
    ``llm_provider_key_last_error`` on failure (last-known-good
    ``last_validated_at`` is preserved on failure). Audit-logs every attempt
    regardless of outcome.

    Imported lazily to break the ``key_storage ↔ anthropic_client`` cycle.
    """
    from gdx_dispatch.core.llm.anthropic_client import LLMNotConfigured, get_client

    model = "claude-haiku-4-5"
    ok = False
    error: str | None = None
    start = time.monotonic()

    try:
        client = get_client(db, tenant_id)
        client.messages.create(
            model=model,
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        ok = True
    except LLMNotConfigured:
        error = "no key set"
    except Exception as exc:  # noqa: BLE001 — Anthropic SDK + httpx + transport — single broad catch is intentional
        error = str(exc)[:500]
    latency_ms = int((time.monotonic() - start) * 1000)

    settings = db.get(TenantSettings, tenant_id)
    if settings is not None:
        if ok:
            settings.llm_provider_key_last_validated_at = _now()
            settings.llm_provider_key_last_error = None
        else:
            settings.llm_provider_key_last_error = error

    log_audit_event_sync(
        db,
        tenant_id=str(tenant_id),
        action="ai_settings.key_tested",
        entity_type="tenant_settings",
        entity_id=str(tenant_id),
        details={"ok": ok, "error": error, "model": model},
    )
    db.commit()

    return {"ok": ok, "model": model, "latency_ms": latency_ms, "error": error}


# Tell pytest not to collect ``test_the_key`` as a test (it's a public helper
# whose name happens to start with ``test_``).
test_the_key.__test__ = False  # type: ignore[attr-defined]
