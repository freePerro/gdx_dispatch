"""SS-30 — cutover event emitters.

Thin helpers that build validated payloads for the four SS-30 events and
append them via :func:`gdx_dispatch.core.events.emit_event`:

* ``gdx_dispatch.cutover.scheduled.v1``              — deprecation drop scheduled
* ``gdx_dispatch.cutover.executed.v1``               — old/new table rename done
* ``gdx_dispatch.cutover.cancelled.v1``              — in-transaction rollback
* ``gdx_dispatch.cutover.deprecated_table_dropped.v1`` — cleanup cron dropped table

Schemas are registered programmatically at import time so the emitters
work in unit tests even when the JSON schema files at
``gdx_dispatch/core/event_schemas/gdx_dispatch.cutover.*.v1.json`` have not been discovered
(slice B can run green before slice C lands the files).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from gdx_dispatch.core.event_catalog import register_schema
from gdx_dispatch.core.events import emit_event

logger = logging.getLogger(__name__)

EVENT_SCHEDULED = "gdx_dispatch.cutover.scheduled.v1"
EVENT_EXECUTED = "gdx_dispatch.cutover.executed.v1"
EVENT_CANCELLED = "gdx_dispatch.cutover.cancelled.v1"
EVENT_DEPRECATED_TABLE_DROPPED = "gdx_dispatch.cutover.deprecated_table_dropped.v1"


_SCHEDULED_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": EVENT_SCHEDULED,
    "description": (
        "Emitted when a deprecated-table drop is scheduled as part of a "
        "cutover. scheduled_drop_at is the earliest moment the cleanup "
        "cron may drop old_table_v1_deprecated."
    ),
    "type": "object",
    "required": [
        "tenant_id",
        "old_table",
        "deprecated_table",
        "scheduled_drop_at",
    ],
    "properties": {
        "tenant_id": {"type": "string"},
        "old_table": {"type": "string"},
        "deprecated_table": {"type": "string"},
        "scheduled_drop_at": {"type": "string"},
        "grace_period_days": {"type": ["integer", "null"]},
        "actor_identity_id": {"type": ["string", "null"]},
        "notes": {"type": ["string", "null"]},
    },
    "additionalProperties": False,
}

_EXECUTED_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": EVENT_EXECUTED,
    "description": (
        "Emitted when a cutover completes: old_table renamed to "
        "old_table_v1_deprecated and new_table renamed to old_table. "
        "dry_run=True payloads are for audit/preview only; no rename "
        "actually happened."
    ),
    "type": "object",
    "required": [
        "tenant_id",
        "old_table",
        "new_table",
        "deprecated_table",
        "executed_at",
        "dry_run",
    ],
    "properties": {
        "tenant_id": {"type": "string"},
        "old_table": {"type": "string"},
        "new_table": {"type": "string"},
        "deprecated_table": {"type": "string"},
        "executed_at": {"type": "string"},
        "dry_run": {"type": "boolean"},
        "actor_identity_id": {"type": ["string", "null"]},
        "scheduled_drop_at": {"type": ["string", "null"]},
        "notes": {"type": ["string", "null"]},
    },
    "additionalProperties": False,
}

_CANCELLED_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": EVENT_CANCELLED,
    "description": (
        "Emitted when run_cutover aborts mid-flight and the transaction "
        "is rolled back. Loud, rare — ops should page on it."
    ),
    "type": "object",
    "required": ["tenant_id", "old_table", "cancelled_at", "reason"],
    "properties": {
        "tenant_id": {"type": "string"},
        "old_table": {"type": "string"},
        "new_table": {"type": ["string", "null"]},
        "cancelled_at": {"type": "string"},
        "reason": {"type": "string"},
        "actor_identity_id": {"type": ["string", "null"]},
        "error_class": {"type": ["string", "null"]},
    },
    "additionalProperties": False,
}

_DROPPED_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": EVENT_DEPRECATED_TABLE_DROPPED,
    "description": (
        "Emitted when the post-cutover cleanup cron drops a "
        "*_v1_deprecated table after its grace period elapsed."
    ),
    "type": "object",
    "required": [
        "tenant_id",
        "deprecated_table",
        "dropped_at",
        "scheduled_drop_at",
    ],
    "properties": {
        "tenant_id": {"type": "string"},
        "old_table": {"type": ["string", "null"]},
        "deprecated_table": {"type": "string"},
        "dropped_at": {"type": "string"},
        "scheduled_drop_at": {"type": "string"},
        "dry_run": {"type": "boolean"},
        "actor_identity_id": {"type": ["string", "null"]},
    },
    "additionalProperties": False,
}


# Register at import so slice B works even before slice C's JSON files
# are discovered on disk. Slice C will register the identical schemas
# from disk — register_schema overwrites, so no conflict.
for _et, _schema in (
    (EVENT_SCHEDULED, _SCHEDULED_SCHEMA),
    (EVENT_EXECUTED, _EXECUTED_SCHEMA),
    (EVENT_CANCELLED, _CANCELLED_SCHEMA),
    (EVENT_DEPRECATED_TABLE_DROPPED, _DROPPED_SCHEMA),
):
    try:
        register_schema(_et, _schema)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "cutover_events: failed to register %s at import: %s",
            _et, exc, exc_info=True,
        )


def _isoformat(dt: datetime | None) -> str:
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def emit_cutover_scheduled(
    db: Any,
    *,
    tenant_id: str,
    old_table: str,
    deprecated_table: str,
    scheduled_drop_at: datetime,
    grace_period_days: int | None = None,
    actor_identity_id: str | None = None,
    notes: str | None = None,
) -> Any:
    """Emit ``gdx_dispatch.cutover.scheduled.v1``."""
    if not tenant_id or not old_table or not deprecated_table:
        raise ValueError(
            "emit_cutover_scheduled: tenant_id/old_table/deprecated_table required"
        )
    payload = {
        "tenant_id": tenant_id,
        "old_table": old_table,
        "deprecated_table": deprecated_table,
        "scheduled_drop_at": _isoformat(scheduled_drop_at),
        "grace_period_days": grace_period_days,
        "actor_identity_id": actor_identity_id,
        "notes": notes,
    }
    logger.info(
        "emit_cutover_scheduled tenant=%s deprecated=%s drop_at=%s",
        tenant_id, deprecated_table, payload["scheduled_drop_at"],
    )
    return emit_event(db, EVENT_SCHEDULED, payload, tenant_id=tenant_id)


def emit_cutover_executed(
    db: Any,
    *,
    tenant_id: str,
    old_table: str,
    new_table: str,
    deprecated_table: str,
    executed_at: datetime | None = None,
    dry_run: bool = False,
    actor_identity_id: str | None = None,
    scheduled_drop_at: datetime | None = None,
    notes: str | None = None,
) -> Any:
    """Emit ``gdx_dispatch.cutover.executed.v1``."""
    if not tenant_id or not old_table or not new_table or not deprecated_table:
        raise ValueError(
            "emit_cutover_executed: tenant_id/old_table/new_table/deprecated_table required"
        )
    payload = {
        "tenant_id": tenant_id,
        "old_table": old_table,
        "new_table": new_table,
        "deprecated_table": deprecated_table,
        "executed_at": _isoformat(executed_at),
        "dry_run": bool(dry_run),
        "actor_identity_id": actor_identity_id,
        "scheduled_drop_at": (
            _isoformat(scheduled_drop_at) if scheduled_drop_at else None
        ),
        "notes": notes,
    }
    log_fn = logger.warning if not dry_run else logger.info
    log_fn(
        "emit_cutover_executed dry_run=%s tenant=%s old=%s new=%s deprecated=%s",
        dry_run, tenant_id, old_table, new_table, deprecated_table,
    )
    return emit_event(db, EVENT_EXECUTED, payload, tenant_id=tenant_id)


def emit_cutover_cancelled(
    db: Any,
    *,
    tenant_id: str,
    old_table: str,
    reason: str,
    new_table: str | None = None,
    cancelled_at: datetime | None = None,
    actor_identity_id: str | None = None,
    error_class: str | None = None,
) -> Any:
    """Emit ``gdx_dispatch.cutover.cancelled.v1``. Loud."""
    if not tenant_id or not old_table or not reason:
        raise ValueError(
            "emit_cutover_cancelled: tenant_id/old_table/reason required"
        )
    payload = {
        "tenant_id": tenant_id,
        "old_table": old_table,
        "new_table": new_table,
        "cancelled_at": _isoformat(cancelled_at),
        "reason": reason,
        "actor_identity_id": actor_identity_id,
        "error_class": error_class,
    }
    logger.error(
        "emit_cutover_cancelled tenant=%s table=%s reason=%s err_class=%s",
        tenant_id, old_table, reason, error_class,
    )
    return emit_event(db, EVENT_CANCELLED, payload, tenant_id=tenant_id)


def emit_deprecated_table_dropped(
    db: Any,
    *,
    tenant_id: str,
    deprecated_table: str,
    scheduled_drop_at: datetime,
    old_table: str | None = None,
    dropped_at: datetime | None = None,
    dry_run: bool = False,
    actor_identity_id: str | None = None,
) -> Any:
    """Emit ``gdx_dispatch.cutover.deprecated_table_dropped.v1``."""
    if not tenant_id or not deprecated_table:
        raise ValueError(
            "emit_deprecated_table_dropped: tenant_id/deprecated_table required"
        )
    payload = {
        "tenant_id": tenant_id,
        "old_table": old_table,
        "deprecated_table": deprecated_table,
        "dropped_at": _isoformat(dropped_at),
        "scheduled_drop_at": _isoformat(scheduled_drop_at),
        "dry_run": bool(dry_run),
        "actor_identity_id": actor_identity_id,
    }
    logger.warning(
        "emit_deprecated_table_dropped dry_run=%s tenant=%s table=%s",
        dry_run, tenant_id, deprecated_table,
    )
    return emit_event(db, EVENT_DEPRECATED_TABLE_DROPPED, payload, tenant_id=tenant_id)
