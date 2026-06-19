"""SS-24 metering + billing model stub additions.

NOTE: INTEGRATION_TODO — these definitions live on a separate declarative
Base (``SS24Base``) and are NOT yet mounted on the primary platform Base
in ``gdx_dispatch/models/platform.py``. The SS-24 Alembic migration
(``TODO_ss24_metering_XXXX.py``) uses ``down_revision = "ss23_event_bus"``
as a placeholder chained directly after SS-23. When SS-24 integration
lands, merge these tables onto the canonical Base and wire the migration
into the main chain (same shape as SS-21 / SS-23 stubs).

Tables:
    - metering_usage          — per-(period, tenant, event_type) counter
    - metering_checkpoint     — idempotency marker for aggregator re-runs
    - billing_plan            — tenant plan + per-event-type limits
    - billing_overage_event   — record of detected overages (one per event
                                emit, for audit + Stripe correlation)

All tables are additive — they do NOT alter ``event_outbox`` or any
existing SS-10 / SS-23 table.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import declarative_base

SS24Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MeteringUsage(SS24Base):
    """Aggregated per-(period, tenant, event_type) counter.

    One row per (period_kind, period_start, tenant_id, event_type). The
    aggregator overwrites ``quantity`` on re-run using the checkpoint
    idempotency guard (see ``MeteringCheckpoint``).
    """

    __tablename__ = "metering_usage"
    __table_args__ = (
        UniqueConstraint(
            "period_kind",
            "period_start",
            "tenant_id",
            "event_type",
            name="uq_metering_usage_period_tenant_event",
        ),
        Index("ix_metering_usage_tenant_period", "tenant_id", "period_start"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    period_kind = Column(String(16), nullable=False)  # hour|day|month
    period_start = Column(DateTime(timezone=True), nullable=False)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    event_type = Column(String(128), nullable=False)
    quantity = Column(BigInteger, nullable=False, default=0)
    aggregated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class MeteringCheckpoint(SS24Base):
    """Per-period aggregator idempotency marker.

    ``last_event_id`` is the ``event_outbox.id`` of the most recent row
    successfully folded into the matching ``MeteringUsage`` bucket. Re-running
    the aggregator for the same period skips any row <= this id within the
    (tenant_id, event_type) group.
    """

    __tablename__ = "metering_checkpoint"
    __table_args__ = (
        UniqueConstraint(
            "period_kind",
            "period_start",
            "tenant_id",
            "event_type",
            name="uq_metering_checkpoint_period_tenant_event",
        ),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    period_kind = Column(String(16), nullable=False)
    period_start = Column(DateTime(timezone=True), nullable=False)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    event_type = Column(String(128), nullable=False)
    last_event_id = Column(Uuid(as_uuid=True), nullable=True)
    last_emitted_at = Column(DateTime(timezone=True), nullable=True)
    quantity_total = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class BillingPlan(SS24Base):
    """Tenant billing plan + per-event-type limits.

    ``limits`` is a JSON blob: ``{"event_type": int_limit_per_period}``.
    ``stripe_subscription_item_id_by_event`` maps event_type → Stripe
    subscription item id for usage-record pushes.
    """

    __tablename__ = "billing_plan"
    __table_args__ = (
        Index("ix_billing_plan_tenant", "tenant_id", unique=True),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    plan_code = Column(String(64), nullable=False, default="free")
    period_kind = Column(String(16), nullable=False, default="month")
    limits = Column(JSON, nullable=False, default=dict)
    stripe_subscription_id = Column(String(128), nullable=True)
    stripe_subscription_item_id_by_event = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class BillingOverageEvent(SS24Base):
    """One row per detected overage.

    Persisted so we can correlate with Stripe invoices and avoid double-
    emitting the ``gdx.billing.overage_detected.v1`` event when the
    aggregator re-runs over a period still in overage state.
    """

    __tablename__ = "billing_overage_event"
    __table_args__ = (
        Index(
            "ix_billing_overage_period_tenant_event",
            "period_kind",
            "period_start",
            "tenant_id",
            "event_type",
            unique=True,
        ),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    event_type = Column(String(128), nullable=False)
    period_kind = Column(String(16), nullable=False)
    period_start = Column(DateTime(timezone=True), nullable=False)
    limit_value = Column(BigInteger, nullable=False)
    observed_quantity = Column(BigInteger, nullable=False)
    detected_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    emitted_event_id = Column(Uuid(as_uuid=True), nullable=True)
    notes = Column(String(512), nullable=True)


# Alias for caller ergonomics — Integer type is used by SQLite fallback
_Integer = Integer  # keep import alive for static analyzers
