"""SS-29 shadow-migration model stub additions.

NOTE: INTEGRATION_TODO — these definitions live on a separate declarative
Base (``SS29Base``) and are NOT yet mounted on the primary platform Base
in ``gdx_dispatch/models/platform.py``. The SS-29 Alembic migration
(``TODO_ss29_shadow_migration_XXXX.py``) is on placeholder
``down_revision = "INTEGRATION_TODO"``.

Tables:
    - shadow_migration_state       — per (tenant, old_table) mode row:
                                     'off' | 'shadow' | 'cutover'
    - shadow_migration_checkpoint  — backfill resume state
    - shadow_migration_drift       — append-only drift evidence

Per the SS-29 plan: shadow-migration is for NEW v2 tables that live
alongside v1; these control-plane tables never touch the data schema.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
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

SS29Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ShadowMigrationState(SS29Base):
    """Per (tenant, old_table) shadow-migration mode row.

    Mode transitions are audit-worthy — see SS-29 events:
    ``gdx.shadow.enabled.v1``, ``gdx.shadow.cutover.v1``, ``gdx.shadow.rollback.v1``.

    ``cutover_at`` stamps the moment /cutover was invoked; ``/rollback`` is
    valid only when ``now - cutover_at < 24h`` (see admin router).
    """

    __tablename__ = "shadow_migration_state"
    __table_args__ = (
        UniqueConstraint("tenant_id", "old_table", name="uq_sms_tenant_table"),
        Index("ix_sms_tenant", "tenant_id"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(String(64), nullable=False)
    old_table = Column(String(128), nullable=False)
    new_table = Column(String(128), nullable=False)
    mode = Column(String(16), nullable=False, default="off")
    cutover_at = Column(DateTime(timezone=True), nullable=True)
    rolled_back_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ShadowMigrationCheckpoint(SS29Base):
    """Backfill resume checkpoint.

    Stores per (tenant, old_table) the last processed row so a crashed
    backfill can resume. ``last_row_pk`` is stringified to support both
    integer and UUID primary keys without two columns.
    """

    __tablename__ = "shadow_migration_checkpoint"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "old_table", name="uq_smc_tenant_table"
        ),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(String(64), nullable=False)
    old_table = Column(String(128), nullable=False)
    last_row_id = Column(BigInteger, nullable=True)
    last_row_pk = Column(String(128), nullable=True)
    row_count_this_session = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ShadowMigrationDrift(SS29Base):
    """Append-only drift evidence row.

    Written by ``ShadowWriter`` whenever a dual-write fails, produces a
    mismatched row, or cannot find the new-table row post-insert.
    """

    __tablename__ = "shadow_migration_drift"
    __table_args__ = (
        Index("ix_smd_tenant_created", "tenant_id", "created_at"),
        Index("ix_smd_table", "old_table"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(String(64), nullable=False)
    old_table = Column(String(128), nullable=False)
    reason = Column(String(64), nullable=False)
    old_hash = Column(String(64), nullable=True)
    new_hash = Column(String(64), nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
