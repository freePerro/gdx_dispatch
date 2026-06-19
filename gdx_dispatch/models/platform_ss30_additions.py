"""SS-30 cutover-cleanup model stub additions.

NOTE: INTEGRATION_TODO — these definitions live on a separate declarative
Base (``SS30Base``) and are NOT yet mounted on the primary platform Base
in ``gdx_dispatch/models/platform.py``. The SS-30 Alembic migration
(``TODO_ss30_cutover_XXXX.py``) is on placeholder
``down_revision = "INTEGRATION_TODO"``.

Tables:
    - cutover_schedule          — per (tenant, old_table) cutover record +
                                  scheduled_drop_at for the cleanup cron
    - deprecated_table_record   — historical ledger of every
                                  *_v1_deprecated table the cron dropped

Both tables are control-plane only — they do not touch data rows.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import declarative_base

SS30Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CutoverSchedule(SS30Base):
    """One row per executed cutover.

    Written by :func:`gdx_dispatch.core.cutover_orchestrator.run_cutover` inside
    the cutover transaction. Holds the ``scheduled_drop_at`` consulted by
    the cleanup cron and the ``/extend-deprecation`` router endpoint.

    ``dropped_at`` is nullable — filled when the cleanup cron actually
    performs the DROP. ``dry_run`` distinguishes audit/preview runs from
    real cutovers.
    """

    __tablename__ = "cutover_schedule"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "old_table", name="uq_cs_tenant_table"
        ),
        Index("ix_cs_scheduled_drop_at", "scheduled_drop_at"),
        Index("ix_cs_tenant", "tenant_id"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(String(64), nullable=False)
    old_table = Column(String(128), nullable=False)
    new_table = Column(String(128), nullable=False)
    deprecated_table = Column(String(160), nullable=False)
    executed_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    scheduled_drop_at = Column(DateTime(timezone=True), nullable=False)
    extended_count = Column(
        # int stored as str avoids SA integer-default idiosyncrasies across DBs
        String(8), nullable=False, default="0"
    )
    dry_run = Column(Boolean, nullable=False, default=False)
    dropped_at = Column(DateTime(timezone=True), nullable=True)
    actor_identity_id = Column(String(64), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class DeprecatedTableRecord(SS30Base):
    """Append-only ledger of dropped deprecated tables.

    Written by :mod:`gdx_dispatch.tools.cutover_cleanup_cron` after a successful
    DROP TABLE. Never mutated. Keeps the audit trail forever even after
    the corresponding ``cutover_schedule`` row is cleaned up.
    """

    __tablename__ = "deprecated_table_record"
    __table_args__ = (
        Index("ix_dtr_tenant", "tenant_id"),
        Index("ix_dtr_dropped_at", "dropped_at"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(String(64), nullable=False)
    old_table = Column(String(128), nullable=True)
    deprecated_table = Column(String(160), nullable=False)
    scheduled_drop_at = Column(DateTime(timezone=True), nullable=False)
    dropped_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    dry_run = Column(Boolean, nullable=False, default=False)
    actor_identity_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
