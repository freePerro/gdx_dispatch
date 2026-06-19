"""SS-33 resource-extensibility model stub additions.

NOTE: TODO — these definitions live on a separate
declarative Base (``SS33Base``) and are NOT yet mounted on the primary
platform Base in ``gdx_dispatch/models/platform.py``. The SS-33 Alembic
migration (``TODO_ss33_resource_extensibility_XXXX.py``) is on
placeholder ``down_revision = "TODO"``. When SS-33
integration lands, merge these onto the canonical Base and wire the
migration into the main chain.

Tables:
    - resource_type                    — tenant-private resource type
                                         declarations; loader.py reads
                                         this at app start.
    - resource_instance                — generic per-type data rows
                                         (one row per instance of any
                                         registered type).
    - resource_type_deletion_request   — 7-day-grace platform type
                                         deletion tracker (super-admin
                                         only; router wires this up at
                                         integration time).

These are intentionally additive: no existing table is altered.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import declarative_base

SS33Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ResourceType(SS33Base):
    """Tenant-private resource type declaration.

    Platform-wide types live on the filesystem
    (``gdx_dispatch/core/resource_types/*.json``) and are NOT stored here; this
    table is for runtime tenant-declared types only.
    """

    __tablename__ = "resource_type"
    __table_args__ = (
        UniqueConstraint("name", name="uq_resource_type_name"),
        Index("ix_resource_type_owner", "owner_tenant_id"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(160), nullable=False)
    json_schema = Column(JSON, nullable=False)
    capabilities = Column(JSON, nullable=False, default=list)
    index_hints = Column(JSON, nullable=False, default=list)
    owner_tenant_id = Column(String(64), nullable=True)  # NULL = platform-wide
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ResourceInstance(SS33Base):
    """Generic per-type data row.

    Scoped by ``tenant_id`` (even for platform-wide types — the type
    descriptor is shared but instance data is not). Soft-delete via
    ``deleted_at``.
    """

    __tablename__ = "resource_instance"
    __table_args__ = (
        Index("ix_resource_instance_tenant_type", "tenant_id", "type_name"),
        Index("ix_resource_instance_type", "type_name"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(String(64), nullable=False)
    type_name = Column(String(160), nullable=False)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class ResourceTypeDeletionRequest(SS33Base):
    """7-day grace-period record for platform-wide type deletion.

    Super-admins open a deletion request; the platform type is removed
    only after ``scheduled_for`` has passed. This table is populated by
    a dedicated super-admin endpoint (out of scope for SS-33 slice F;
    router wires this up at integration time).
    """

    __tablename__ = "resource_type_deletion_request"
    __table_args__ = (
        UniqueConstraint("name", name="uq_rtdr_name"),
        Index("ix_rtdr_scheduled_for", "scheduled_for"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(160), nullable=False)
    requested_by_identity_id = Column(String(64), nullable=False)
    reason = Column(Text, nullable=True)
    requested_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    scheduled_for = Column(DateTime(timezone=True), nullable=False)
    executed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
