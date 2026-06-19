"""Event outbox helper primitive (SS-10 Slice B).

Single bounded helper: append one EventOutbox row to a caller-owned session.
The caller owns the transaction boundary — this helper NEVER commits.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from gdx_dispatch.models.platform_extensions import EventOutbox


def emit_event(
    db: Session,
    event_name: str,
    payload: dict[str, Any],
    tenant_id: str | UUID | None = None,
    installation_id: UUID | None = None,
    source_event_id: UUID | None = None,
) -> EventOutbox:
    """Append one EventOutbox row to the caller's open session.

    Caller owns the transaction; this helper does not flush or commit. If
    ``source_event_id`` is omitted, a Python-side UUID is generated so the
    caller can correlate the emitted row before committing.

    D97: ``tenant_id`` accepts both UUID and stringified UUID for caller
    convenience (``Principal.tenant_id`` is a string). Coerces here so
    the column-level ``Uuid`` type sees a UUID instance.
    """
    tenant_uuid: UUID | None = None
    if tenant_id is not None:
        if isinstance(tenant_id, UUID):
            tenant_uuid = tenant_id
        else:
            try:
                tenant_uuid = UUID(str(tenant_id))
            except (ValueError, TypeError):
                tenant_uuid = None
    row = EventOutbox(
        event_name=event_name,
        payload=payload,
        tenant_id=tenant_uuid,
        installation_id=installation_id,
        source_event_id=source_event_id if source_event_id is not None else uuid4(),
    )
    db.add(row)
    return row
