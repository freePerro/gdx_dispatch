"""SS-23 event-bus model stub additions.

NOTE: INTEGRATION_TODO — these definitions live on a separate
declarative Base (``SS23Base``) and are NOT yet mounted on the primary
platform Base in ``gdx_dispatch/models/platform.py``. The SS-23 Alembic
migration (``TODO_ss23_event_bus_XXXX.py``) is on placeholder
``down_revision = "INTEGRATION_TODO"``. When SS-24 integration lands,
merge these tables onto the canonical Base and wire the migration into
the main chain.

Tables:
    - event_subscription         — per-installation subscription rows
    - event_drain_checkpoint     — drain worker state per event_outbox row

These are intentionally additive: they do NOT alter ``event_outbox``.
SS-10's outbox schema remains the durable source of truth; the drain
records its own lifecycle bookkeeping in ``event_drain_checkpoint``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import declarative_base

SS23Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EventSubscription(SS23Base):
    """Per-installation event-type subscription."""

    __tablename__ = "event_subscription"
    __table_args__ = (
        Index("ix_event_subscription_install", "installation_id"),
        Index("ix_event_subscription_event_name", "event_name"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    installation_id = Column(Uuid(as_uuid=True), nullable=False)
    event_name = Column(String(128), nullable=False)
    webhook_url = Column(String(1024), nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class EventDrainCheckpoint(SS23Base):
    """Drain worker bookkeeping for one ``event_outbox`` row.

    status values:
        pending     — default; not yet attempted
        delivered   — all sinks returned success (mirrors event_outbox.delivered_at)
        retry       — at least one sink failed; retry_after set
        dead_letter — retry_count exceeded MAX_RETRIES
    """

    __tablename__ = "event_drain_checkpoint"
    __table_args__ = (
        Index("ix_event_drain_checkpoint_event", "event_outbox_id", unique=True),
        Index("ix_event_drain_checkpoint_status", "status"),
        Index("ix_event_drain_checkpoint_retry_after", "retry_after"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event_outbox_id = Column(Uuid(as_uuid=True), nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    retry_count = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    retry_after = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
