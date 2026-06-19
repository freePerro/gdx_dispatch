"""SS-24 Slice A: Metering aggregator.

Walks ``event_outbox`` rows and aggregates them into per-tenant,
per-event-type usage counters for a metering period (hour / day / month).

Contract:
    - Period-scoped, idempotent aggregation. A ``metering_checkpoint`` row
      keyed by (period_kind, period_start, tenant_id, event_type) tracks the
      last ``event_outbox.id`` (``last_event_id``) successfully aggregated.
      Re-running the same period walks only rows with ``id > last_event_id``
      within the period window, so repeated invocations do NOT double-count.
    - NEVER commits. Caller owns the transaction boundary (matches the SS-10
      ``emit_event`` / SS-23 ``drain_once`` discipline).
    - Fails loud: bad period_kind raises ValueError; DB errors propagate.

Period windowing:
    - ``hour``:  floor(emitted_at, hour)       … exclusive upper bound +1h
    - ``day``:   floor(emitted_at, day UTC)    … exclusive upper bound +1d
    - ``month``: floor(emitted_at, month UTC)  … exclusive upper bound +1mo

TODO:
    - The aggregator writes to the ``metering_usage`` + ``metering_checkpoint``
      tables defined in ``gdx_dispatch.models.platform_ss24_additions``; those live
      on a separate Base until SS-24 integration wires them onto the primary
      platform Base (same pattern as SS-21 / SS-23).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.models.platform_extensions import EventOutbox

logger = logging.getLogger(__name__)


VALID_PERIODS = ("hour", "day", "month")


# ── period math ─────────────────────────────────────────────────────────────


def floor_period(ts: datetime, period_kind: str) -> datetime:
    """Return the UTC-aligned start of the period containing ``ts``."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    if period_kind == "hour":
        return ts.replace(minute=0, second=0, microsecond=0)
    if period_kind == "day":
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    if period_kind == "month":
        return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(
        f"invalid period_kind {period_kind!r}; expected one of {VALID_PERIODS}"
    )


def period_end(period_start: datetime, period_kind: str) -> datetime:
    """Exclusive upper bound for ``period_start``."""
    if period_kind == "hour":
        return period_start + timedelta(hours=1)
    if period_kind == "day":
        return period_start + timedelta(days=1)
    if period_kind == "month":
        # month rollover: add 31 days then floor to 1st-of-month
        rough = period_start + timedelta(days=32)
        return rough.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"invalid period_kind {period_kind!r}")


# ── aggregator ──────────────────────────────────────────────────────────────


@dataclass
class AggregatedBucket:
    tenant_id: str | None
    event_type: str
    period_kind: str
    period_start: datetime
    quantity: int
    last_event_id: UUID
    last_emitted_at: datetime
    first_event_id: UUID


@dataclass
class AggregateStats:
    scanned: int = 0
    buckets: int = 0
    buckets_written: int = 0
    checkpoints_upserted: int = 0
    skipped_no_new_events: int = 0
    # keep the buckets around so pipeline can push them to sinks
    aggregated: list[AggregatedBucket] = field(default_factory=list)


def _get_checkpoint(
    db: Session,
    *,
    period_kind: str,
    period_start: datetime,
    tenant_id: str | None,
    event_type: str,
):
    from gdx_dispatch.models.platform_ss24_additions import MeteringCheckpoint  # lazy

    return db.scalar(
        select(MeteringCheckpoint).where(
            MeteringCheckpoint.period_kind == period_kind,
            MeteringCheckpoint.period_start == period_start,
            MeteringCheckpoint.tenant_id == _coerce_tenant(tenant_id),
            MeteringCheckpoint.event_type == event_type,
        )
    )


def _upsert_checkpoint(
    db: Session,
    *,
    period_kind: str,
    period_start: datetime,
    tenant_id: str | None,
    event_type: str,
    last_event_id: UUID,
    last_emitted_at: datetime,
    quantity_total: int,
) -> None:
    from gdx_dispatch.models.platform_ss24_additions import MeteringCheckpoint  # lazy

    row = _get_checkpoint(
        db,
        period_kind=period_kind,
        period_start=period_start,
        tenant_id=tenant_id,
        event_type=event_type,
    )
    now = datetime.now(timezone.utc)
    if row is None:
        row = MeteringCheckpoint(
            period_kind=period_kind,
            period_start=period_start,
            tenant_id=_coerce_tenant(tenant_id),
            event_type=event_type,
            last_event_id=last_event_id,
            last_emitted_at=last_emitted_at,
            quantity_total=quantity_total,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row.last_event_id = last_event_id
        row.last_emitted_at = last_emitted_at
        row.quantity_total = quantity_total
        row.updated_at = now


def _coerce_tenant(value: str | UUID | None) -> UUID | None:
    """D97: aggregator buckets carry str|None tenant_id; the column is now Uuid."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def _upsert_usage(
    db: Session,
    bucket: AggregatedBucket,
) -> None:
    from gdx_dispatch.models.platform_ss24_additions import MeteringUsage  # lazy

    tenant_uuid = _coerce_tenant(bucket.tenant_id)
    row = db.scalar(
        select(MeteringUsage).where(
            MeteringUsage.period_kind == bucket.period_kind,
            MeteringUsage.period_start == bucket.period_start,
            MeteringUsage.tenant_id == tenant_uuid,
            MeteringUsage.event_type == bucket.event_type,
        )
    )
    now = datetime.now(timezone.utc)
    if row is None:
        row = MeteringUsage(
            period_kind=bucket.period_kind,
            period_start=bucket.period_start,
            tenant_id=tenant_uuid,
            event_type=bucket.event_type,
            quantity=bucket.quantity,
            aggregated_at=now,
        )
        db.add(row)
    else:
        # checkpoint-driven: we always represent the cumulative total for the
        # period, so overwrite on re-run. Idempotent by construction.
        row.quantity = bucket.quantity
        row.aggregated_at = now


def aggregate_period(
    db: Session,
    *,
    period_kind: str,
    period_start: datetime,
    event_types: Iterable[str] | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> AggregateStats:
    """Aggregate every ``event_outbox`` row whose ``emitted_at`` falls inside
    ``[period_start, period_end)`` into per-(tenant, event_type) buckets.

    Idempotent: uses ``metering_checkpoint.last_event_id`` to skip rows already
    aggregated for this period/tenant/event_type combo.

    Caller owns commit.

    Red-team Pattern 6 (metering_aggregator.py:215): the row scan had
    no bound — a period with millions of events would try to aggregate
    them all in one transaction. ``offset`` / ``limit`` are optional
    (default behaviour: scan everything, preserving existing callers)
    and let operators drain huge periods in chunks. The idempotency
    checkpoint + ``ORDER BY (emitted_at, id)`` guarantee chunked runs
    converge to the same result as one full run.
    """
    if period_kind not in VALID_PERIODS:
        raise ValueError(
            f"invalid period_kind {period_kind!r}; expected one of {VALID_PERIODS}"
        )
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0 or None")
    start = floor_period(period_start, period_kind)
    end = period_end(start, period_kind)

    # ORDER BY already present — red-team only flagged the pagination
    # gap here. Kept inline for clarity.
    stmt = (
        select(EventOutbox)
        .where(EventOutbox.emitted_at >= start)
        .where(EventOutbox.emitted_at < end)
        .order_by(EventOutbox.emitted_at, EventOutbox.id)
    )
    if event_types is not None:
        ets = tuple(event_types)
        if not ets:
            return AggregateStats()
        stmt = stmt.where(EventOutbox.event_name.in_(ets))

    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)

    rows = db.execute(stmt).scalars().all()

    stats = AggregateStats()
    stats.scanned = len(rows)

    # group by (tenant_id, event_type)
    groups: dict[tuple[str | None, str], list[EventOutbox]] = {}
    for r in rows:
        key = (r.tenant_id, r.event_name)
        groups.setdefault(key, []).append(r)

    for (tenant_id, event_type), group_rows in groups.items():
        stats.buckets += 1
        # idempotency: re-check existing checkpoint
        cp = _get_checkpoint(
            db,
            period_kind=period_kind,
            period_start=start,
            tenant_id=tenant_id,
            event_type=event_type,
        )
        # filter rows that haven't been aggregated before — rows are ordered
        # by (emitted_at, id). A row is "new" iff (emitted_at, id) is strictly
        # greater than the checkpoint's (last_emitted_at, last_event_id) tuple.
        if cp is not None and cp.last_emitted_at is not None:
            cp_emit = cp.last_emitted_at
            if cp_emit.tzinfo is None:
                cp_emit = cp_emit.replace(tzinfo=timezone.utc)
            cp_id = cp.last_event_id

            def _is_new(r):
                r_emit = r.emitted_at
                if r_emit.tzinfo is None:
                    r_emit = r_emit.replace(tzinfo=timezone.utc)
                if r_emit > cp_emit:
                    return True
                if r_emit == cp_emit and cp_id is not None:
                    # tiebreak on UUID lex order (bytes)
                    return r.id.bytes > cp_id.bytes
                return False

            new_rows = [r for r in group_rows if _is_new(r)]
            already_counted = cp.quantity_total or 0
        else:
            new_rows = group_rows
            already_counted = 0

        if not new_rows:
            stats.skipped_no_new_events += 1
            continue

        added_quantity = len(new_rows)
        total_quantity = already_counted + added_quantity
        last_event = new_rows[-1]
        first_event = new_rows[0]
        last_emit = last_event.emitted_at
        if last_emit.tzinfo is None:
            last_emit = last_emit.replace(tzinfo=timezone.utc)

        bucket = AggregatedBucket(
            tenant_id=tenant_id,
            event_type=event_type,
            period_kind=period_kind,
            period_start=start,
            quantity=total_quantity,
            last_event_id=last_event.id,
            last_emitted_at=last_emit,
            first_event_id=first_event.id,
        )
        stats.aggregated.append(bucket)

        _upsert_usage(db, bucket)
        stats.buckets_written += 1

        _upsert_checkpoint(
            db,
            period_kind=period_kind,
            period_start=start,
            tenant_id=tenant_id,
            event_type=event_type,
            last_event_id=last_event.id,
            last_emitted_at=last_emit,
            quantity_total=total_quantity,
        )
        stats.checkpoints_upserted += 1

    logger.info(
        "metering_aggregator: period=%s start=%s scanned=%d buckets=%d written=%d",
        period_kind,
        start.isoformat(),
        stats.scanned,
        stats.buckets,
        stats.buckets_written,
    )
    return stats


def aggregate_current(db: Session, *, period_kind: str) -> AggregateStats:
    """Aggregate the period containing ``now``."""
    return aggregate_period(
        db,
        period_kind=period_kind,
        period_start=datetime.now(timezone.utc),
    )
