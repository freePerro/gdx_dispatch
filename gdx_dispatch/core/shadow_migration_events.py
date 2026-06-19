"""SS-29 slice C — shadow migration event emitters.

Thin helpers that build validated payloads for the four SS-29 events and
append them via :func:`gdx_dispatch.core.events.emit_event`. Each helper is a
pure function: it constructs the payload dict, validates the required
keys are present (fail-loud), and delegates the write.

INTEGRATION_TODO: at main-chain integration, these helpers will be wired
into the admin router (slice F) and the ShadowWriter drift path (slice B).
The payload shapes match the schemas at
``gdx_dispatch/core/event_schemas/gdx.shadow.*.v1.json``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from gdx_dispatch.core.events import emit_event

logger = logging.getLogger(__name__)

EVENT_ENABLED = "gdx_dispatch.shadow.enabled.v1"
EVENT_CUTOVER = "gdx_dispatch.shadow.cutover.v1"
EVENT_ROLLBACK = "gdx_dispatch.shadow.rollback.v1"
EVENT_DRIFT = "gdx_dispatch.shadow.drift_detected.v1"

_VALID_DRIFT_REASONS = frozenset(
    {"hash_mismatch", "new_row_missing", "insert_failed", "transform_failed"}
)


def _isoformat(dt: datetime | None) -> str:
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def emit_shadow_enabled(
    db: Any,
    *,
    tenant_id: str,
    old_table: str,
    new_table: str,
    actor_identity_id: str | None = None,
    enabled_at: datetime | None = None,
    notes: str | None = None,
) -> Any:
    """Emit ``gdx.shadow.enabled.v1``."""
    if not tenant_id or not old_table or not new_table:
        raise ValueError("emit_shadow_enabled: tenant_id/old_table/new_table required")

    payload = {
        "tenant_id": tenant_id,
        "old_table": old_table,
        "new_table": new_table,
        "enabled_at": _isoformat(enabled_at),
        "actor_identity_id": actor_identity_id,
        "notes": notes,
    }
    logger.info("emit_shadow_enabled tenant=%s table=%s", tenant_id, old_table)
    return emit_event(db, EVENT_ENABLED, payload, tenant_id=tenant_id)


def emit_shadow_cutover(
    db: Any,
    *,
    tenant_id: str,
    old_table: str,
    new_table: str,
    actor_identity_id: str | None = None,
    cutover_at: datetime | None = None,
    notes: str | None = None,
) -> Any:
    """Emit ``gdx.shadow.cutover.v1``."""
    if not tenant_id or not old_table or not new_table:
        raise ValueError("emit_shadow_cutover: tenant_id/old_table/new_table required")

    payload = {
        "tenant_id": tenant_id,
        "old_table": old_table,
        "new_table": new_table,
        "cutover_at": _isoformat(cutover_at),
        "actor_identity_id": actor_identity_id,
        "notes": notes,
    }
    logger.warning(
        "emit_shadow_cutover: IRREVERSIBLE-after-24h tenant=%s table=%s",
        tenant_id, old_table,
    )
    return emit_event(db, EVENT_CUTOVER, payload, tenant_id=tenant_id)


def emit_shadow_rollback(
    db: Any,
    *,
    tenant_id: str,
    old_table: str,
    new_table: str,
    cutover_at: datetime,
    actor_identity_id: str | None = None,
    rolled_back_at: datetime | None = None,
    reason: str | None = None,
) -> Any:
    """Emit ``gdx.shadow.rollback.v1``. Loud — ops should page on it."""
    if not tenant_id or not old_table or not new_table:
        raise ValueError("emit_shadow_rollback: tenant_id/old_table/new_table required")

    payload = {
        "tenant_id": tenant_id,
        "old_table": old_table,
        "new_table": new_table,
        "cutover_at": _isoformat(cutover_at),
        "rolled_back_at": _isoformat(rolled_back_at),
        "actor_identity_id": actor_identity_id,
        "reason": reason,
    }
    logger.error(
        "emit_shadow_rollback: ROLLBACK tenant=%s table=%s reason=%s",
        tenant_id, old_table, reason,
    )
    return emit_event(db, EVENT_ROLLBACK, payload, tenant_id=tenant_id)


def emit_shadow_drift_detected(
    db: Any,
    *,
    tenant_id: str,
    old_table: str,
    reason: str,
    new_table: str | None = None,
    old_hash: str | None = None,
    new_hash: str | None = None,
    detected_at: datetime | None = None,
    drift_row_id: UUID | str | None = None,
) -> Any:
    """Emit ``gdx.shadow.drift_detected.v1``."""
    if not tenant_id or not old_table:
        raise ValueError("emit_shadow_drift_detected: tenant_id/old_table required")
    if reason not in _VALID_DRIFT_REASONS:
        raise ValueError(
            f"emit_shadow_drift_detected: invalid reason {reason!r}; "
            f"expected one of {sorted(_VALID_DRIFT_REASONS)}"
        )

    payload = {
        "tenant_id": tenant_id,
        "old_table": old_table,
        "new_table": new_table,
        "reason": reason,
        "old_hash": old_hash,
        "new_hash": new_hash,
        "detected_at": _isoformat(detected_at),
        "drift_row_id": str(drift_row_id) if drift_row_id is not None else None,
    }
    logger.error(
        "emit_shadow_drift_detected tenant=%s table=%s reason=%s",
        tenant_id, old_table, reason,
    )
    return emit_event(db, EVENT_DRIFT, payload, tenant_id=tenant_id)
