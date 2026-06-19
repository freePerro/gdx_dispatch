"""Phone.com does not sign webhooks (Slice 0 finding 2026-04-27). This module implements
URL-path-secret + payload voip_id validation. If Phone.com adds signing later, swap
verify_webhook_path for an HMAC verifier and keep verify_payload_voip_id as
belt-and-suspenders.

P1.4 (2026-05-04): adds a previous-secret grace window. The rotation task PATCHes
the callback URL on Phone.com to a new secret, but in-flight retries Phone.com
is still delivering against the old URL must keep working until ``prev_until``
passes (default 1 hour after rotation).
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from gdx_dispatch.control.models import TenantSettings
from gdx_dispatch.modules.phone_com.key_storage import _fernet

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebhookAuthDecision:
    """The result of a webhook authentication attempt."""
    accepted: bool
    reason: str
    status_code: int


def _decrypt_or_none(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except Exception:
        log.warning("phone_com.webhook_secret_decrypt_failed", exc_info=True)
        return None


def verify_webhook_path(tenant_id: UUID, path_secret: str, db: Session) -> bool:
    """Verifies the provided path_secret against the stored tenant webhook secret.

    Accepts either the current ``phone_com_webhook_secret`` or, if the
    rotation grace window is active, the previous secret in
    ``phone_com_webhook_secret_prev`` (until ``prev_until`` passes).
    Uses constant-time comparison to prevent timing attacks.
    """
    settings = db.get(TenantSettings, tenant_id)
    if settings is None:
        return False

    current = _decrypt_or_none(settings.phone_com_webhook_secret)
    if current is not None and secrets.compare_digest(current, path_secret):
        return True

    prev = _decrypt_or_none(getattr(settings, "phone_com_webhook_secret_prev", None))
    prev_until = getattr(settings, "phone_com_webhook_secret_prev_until", None)
    if prev is None or prev_until is None:
        return False
    # SQLite drops tzinfo on DateTime(timezone=True) round-trip; assume UTC
    # in that case to keep the comparison safe across both backends.
    if prev_until.tzinfo is None:
        prev_until = prev_until.replace(tzinfo=timezone.utc)
    if prev_until <= datetime.now(timezone.utc):
        return False
    return secrets.compare_digest(prev, path_secret)


def verify_payload_voip_id(payload: dict, expected_voip_id: int) -> bool:
    """Checks if the payload contains the expected voip_id.

    Coerces both the payload value and the expected value to integers for comparison.
    Returns False if the key is missing or values do not match.
    """
    val = payload.get("voip_id")
    if val is None:
        return False

    try:
        return int(val) == int(expected_voip_id)
    except (ValueError, TypeError):
        return False


def decide(
    tenant_id: UUID,
    path_secret: str,
    payload: dict | None,
    db: Session,
    expected_voip_id: int | None,
) -> WebhookAuthDecision:
    """The canonical end-to-end decision engine for webhook authentication.

    Logic:
    1. Check path secret. If mismatch, 404 (hide endpoint existence).
    2. If payload is None, it's a test ping (204).
    3. If expected_voip_id is provided, check payload. If mismatch, 400.
    4. Otherwise, accept (204).
    """
    # 1. Verify Path
    if not verify_webhook_path(tenant_id, path_secret, db):
        return WebhookAuthDecision(False, "path secret mismatch", 404)

    # 2. Handle Test Pings
    if payload is None:
        return WebhookAuthDecision(True, "test ping", 204)

    # 3. Verify payload voip_id if required
    if expected_voip_id is not None and not verify_payload_voip_id(payload, expected_voip_id):
        return WebhookAuthDecision(False, "voip_id mismatch", 400)

    # 4. Success
    return WebhookAuthDecision(True, "ok", 204)
