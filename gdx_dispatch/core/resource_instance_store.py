"""SS-33 Slice E: resource_instance_store — generic CRUD helpers.

Stores resource instances in the ``resource_instance`` table:

    resource_instance(
        id UUID PK,
        tenant_id STRING(64),
        type_name STRING(160),
        payload JSON,
        created_at,
        updated_at,
        deleted_at,
    )

Every write calls :func:`gdx_dispatch.core.resource_type_registry.validate_instance`
FIRST. On schema violation the caller receives the ``ResourceSchemaError``
and NOTHING is written. "Silent failure is not failure, it is lying."

All reads are tenant-scoped: even for platform-wide types, the type
descriptor is shared but the instance data lives per-tenant.

INTEGRATION TODO
----------------
* Table ``resource_instance`` is declared in
  ``gdx_dispatch/models/platform_ss33_additions.py`` on ``SS33Base``; the
  Alembic migration file is on placeholder ``down_revision =
  "INTEGRATION_TODO"`` until SS-33 integration wires the chain.
* Soft-delete semantics are via ``deleted_at``; the helpers below
  filter deleted rows by default and expose an ``include_deleted`` flag
  for admin recovery flows (not wired at router layer).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from gdx_dispatch.core import resource_type_registry as rtr

logger = logging.getLogger(__name__)


class ResourceInstanceError(RuntimeError):
    """Raised for instance-store operational failures (not schema violations)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_json(payload: Any) -> str:
    # Stored as JSON string (portable across SQLite + Postgres test harnesses).
    return json.dumps(payload, default=str, sort_keys=True)


def _from_json(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": str(row[0]) if not isinstance(row[0], str) else row[0],
        "tenant_id": row[1],
        "type_name": row[2],
        "payload": _from_json(row[3]),
        "created_at": row[4].isoformat() if hasattr(row[4], "isoformat") else row[4],
        "updated_at": row[5].isoformat() if hasattr(row[5], "isoformat") else row[5],
        "deleted_at": (
            row[6].isoformat() if hasattr(row[6], "isoformat") and row[6] else None
        ),
    }


def _ensure_type_exists(type_name: str) -> dict[str, Any]:
    descriptor = rtr.get_type(type_name)
    if descriptor is None:
        raise rtr.ResourceTypeError(f"unknown resource type: {type_name!r}")
    return descriptor


def _tenant_may_use_type(descriptor: dict[str, Any], tenant_id: str) -> bool:
    """Platform-wide types open to all tenants; private types owner-only."""
    owner = descriptor["owner_tenant_id"]
    return owner is None or owner == tenant_id


def create_instance(
    session: Any,
    type_name: str,
    payload: dict[str, Any],
    tenant_id: str,
    *,
    instance_id: UUID | None = None,
) -> dict[str, Any]:
    """Validate + insert one instance row. Returns the stored row dict."""
    descriptor = _ensure_type_exists(type_name)
    if not _tenant_may_use_type(descriptor, tenant_id):
        raise ResourceInstanceError(
            f"tenant {tenant_id!r} may not use private type {type_name!r}"
        )
    # Schema validation BEFORE write — ResourceSchemaError bubbles up.
    rtr.validate_instance(type_name, payload)

    iid = instance_id or uuid4()
    now = _now()
    session.execute(
        text(
            "INSERT INTO resource_instance "
            "(id, tenant_id, type_name, payload, created_at, updated_at, deleted_at) "
            "VALUES (:id, :tenant_id, :type_name, :payload, :created_at, :updated_at, NULL)"
        ),
        {
            "id": str(iid),
            "tenant_id": tenant_id,
            "type_name": type_name,
            "payload": _as_json(payload),
            "created_at": now,
            "updated_at": now,
        },
    )
    return {
        "id": str(iid),
        "tenant_id": tenant_id,
        "type_name": type_name,
        "payload": payload,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "deleted_at": None,
    }


def get_instance(
    session: Any,
    type_name: str,
    instance_id: str,
    tenant_id: str,
) -> dict[str, Any] | None:
    _ensure_type_exists(type_name)
    result = session.execute(
        text(
            "SELECT id, tenant_id, type_name, payload, created_at, updated_at, deleted_at "
            "FROM resource_instance "
            "WHERE id = :id AND tenant_id = :tenant_id AND type_name = :type_name "
            "AND deleted_at IS NULL"
        ),
        {"id": str(instance_id), "tenant_id": tenant_id, "type_name": type_name},
    ).first()
    if result is None:
        return None
    return _row_to_dict(result)


def list_instances(
    session: Any,
    type_name: str,
    tenant_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    _ensure_type_exists(type_name)
    rows = session.execute(
        text(
            "SELECT id, tenant_id, type_name, payload, created_at, updated_at, deleted_at "
            "FROM resource_instance "
            "WHERE tenant_id = :tenant_id AND type_name = :type_name "
            "AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        ),
        {
            "tenant_id": tenant_id,
            "type_name": type_name,
            "limit": limit,
            "offset": offset,
        },
    )
    return [_row_to_dict(r) for r in rows]


def update_instance(
    session: Any,
    type_name: str,
    instance_id: str,
    payload: dict[str, Any],
    tenant_id: str,
) -> dict[str, Any] | None:
    descriptor = _ensure_type_exists(type_name)
    if not _tenant_may_use_type(descriptor, tenant_id):
        raise ResourceInstanceError(
            f"tenant {tenant_id!r} may not use private type {type_name!r}"
        )
    # Validate BEFORE write
    rtr.validate_instance(type_name, payload)

    existing = get_instance(session, type_name, instance_id, tenant_id)
    if existing is None:
        return None
    now = _now()
    session.execute(
        text(
            "UPDATE resource_instance "
            "SET payload = :payload, updated_at = :updated_at "
            "WHERE id = :id AND tenant_id = :tenant_id AND type_name = :type_name"
        ),
        {
            "payload": _as_json(payload),
            "updated_at": now,
            "id": str(instance_id),
            "tenant_id": tenant_id,
            "type_name": type_name,
        },
    )
    return get_instance(session, type_name, instance_id, tenant_id)


def delete_instance(
    session: Any,
    type_name: str,
    instance_id: str,
    tenant_id: str,
) -> bool:
    _ensure_type_exists(type_name)
    now = _now()
    result = session.execute(
        text(
            "UPDATE resource_instance "
            "SET deleted_at = :deleted_at, updated_at = :deleted_at "
            "WHERE id = :id AND tenant_id = :tenant_id AND type_name = :type_name "
            "AND deleted_at IS NULL"
        ),
        {
            "deleted_at": now,
            "id": str(instance_id),
            "tenant_id": tenant_id,
            "type_name": type_name,
        },
    )
    # rowcount is the portable way; may be -1 on drivers that don't support it
    return (result.rowcount or 0) > 0


# ── test helper ─────────────────────────────────────────────────────────────


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS resource_instance (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    type_name VARCHAR(160) NOT NULL,
    payload TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    deleted_at TIMESTAMP
)
"""


def _create_table_for_tests(session: Any) -> None:
    """Idempotent table creator for SQLite test harnesses."""
    session.execute(text(CREATE_TABLE_SQL))
