"""SS-23 Slice C: Event drain worker.

Drains ``event_outbox`` → downstream sinks (webhook delivery, metering
pipeline stub, audit-log-append stub). One pass = one ``drain_once()``
call; the systemd-timer-friendly CLI in ``gdx_dispatch/tools/event_drain_cron.py``
wraps it.

Contract:
    - Reads rows where ``delivered_at IS NULL``. The existing SS-10
      EventOutbox schema is untouched here; retry bookkeeping lives in
      ``EventDrainCheckpoint`` (see platform_ss23_additions.py).
    - Idempotent: re-running on the same row is safe. Sinks that have
      already observed a row should no-op; the drain itself transitions
      state by the checkpoint row keyed on ``event_outbox_id``.
    - Fail loud: every sink exception → ``logger.exception`` +
      increment retry counter + checkpoint ``status='retry'`` with
      ``retry_after``. No silent swallow.
    - Only sets ``delivered_at`` when *every* registered sink returned
      success on this pass. Matches the SS-23 P40 guidance that no
      single lifecycle field lies about delivery scope.

TODO:
    - Wire ``EventDrainCheckpoint`` migration into main alembic chain.
    - Register real sinks (outbound webhook, metering pipe) in SS-24;
      for now ``DEFAULT_SINKS`` ships only no-op audit + webhook stubs
      so the drain can run in tests and staging without side-effects.
    - Replace datetime.utcnow retry_after with a configurable backoff.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.models.platform_extensions import EventOutbox

logger = logging.getLogger(__name__)


# Sink contract: (event_row) -> None. Raise on failure. Must be idempotent.
Sink = Callable[[EventOutbox], None]


# ── default sinks (safe no-op stubs) ────────────────────────────────────────


def _sink_audit_log(event: EventOutbox) -> None:
    """Default audit sink — structured log line. Real impl: insert AuditLog row."""
    logger.info(
        "event_drain.audit event_name=%s source_event_id=%s tenant=%s",
        event.event_name,
        event.source_event_id,
        event.tenant_id,
    )


def _sink_webhook_stub(event: EventOutbox) -> None:
    """Default webhook sink — no-op placeholder.

    Real delivery (matching registered event_subscription rows and POSTing
    to each installation's webhook URL) lands in SS-24. Kept as a stub so
    the drain wiring is exercised end-to-end without network side-effects.
    """
    logger.debug(
        "event_drain.webhook_stub event=%s no-subscribers-impl (SS-24)",
        event.event_name,
    )


def _sink_metering_stub(event: EventOutbox) -> None:
    """Default metering pipeline sink — no-op placeholder (SS-25 owns)."""
    logger.debug("event_drain.metering_stub event=%s", event.event_name)


DEFAULT_SINKS: tuple[Sink, ...] = (
    _sink_audit_log,
    _sink_webhook_stub,
    _sink_metering_stub,
)


# ── retry policy ────────────────────────────────────────────────────────────

DEFAULT_BACKOFF_SECONDS = (30, 120, 600, 3600)  # 30s, 2m, 10m, 1h
MAX_RETRIES = len(DEFAULT_BACKOFF_SECONDS)


def _next_retry_after(retry_count: int) -> datetime:
    idx = min(retry_count, len(DEFAULT_BACKOFF_SECONDS) - 1)
    return datetime.now(timezone.utc) + timedelta(
        seconds=DEFAULT_BACKOFF_SECONDS[idx]
    )


# ── drain pass ──────────────────────────────────────────────────────────────


@dataclass
class DrainStats:
    scanned: int = 0
    delivered: int = 0
    retried: int = 0
    skipped: int = 0  # row is in retry cooldown


def _load_checkpoint(db: Session, event_id: Any) -> Any:
    """Load or None the checkpoint row for an event_outbox id.

    Imported locally so the drain module doesn't hard-require the
    ss23_additions model at import time (the stub is on its own Base).
    """
    from gdx_dispatch.models.platform_ss23_additions import EventDrainCheckpoint  # lazy

    return db.scalar(
        select(EventDrainCheckpoint).where(
            EventDrainCheckpoint.event_outbox_id == event_id
        )
    )


def _upsert_checkpoint(
    db: Session,
    event_id: Any,
    *,
    status: str,
    retry_count: int,
    last_error: str | None,
    retry_after: datetime | None,
) -> None:
    from gdx_dispatch.models.platform_ss23_additions import EventDrainCheckpoint  # lazy

    row = db.scalar(
        select(EventDrainCheckpoint).where(
            EventDrainCheckpoint.event_outbox_id == event_id
        )
    )
    now = datetime.now(timezone.utc)
    if row is None:
        row = EventDrainCheckpoint(
            event_outbox_id=event_id,
            status=status,
            retry_count=retry_count,
            last_error=last_error,
            retry_after=retry_after,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row.status = status
        row.retry_count = retry_count
        row.last_error = last_error
        row.retry_after = retry_after
        row.updated_at = now


def drain_once(
    db: Session,
    sinks: Iterable[Sink] | None = None,
    *,
    batch_size: int = 100,
    offset: int = 0,
) -> DrainStats:
    """Run one drain pass. Caller owns commit.

    For every unsent EventOutbox row (in cooldown rows are skipped),
    calls every sink. If *all* succeed, sets ``delivered_at`` and marks
    checkpoint ``status='delivered'``. If any raises, increments retry
    counter, stamps ``retry_after``, logs, and moves on.

    Red-team Pattern 7 (event_drain.py:65): the scan had no ORDER BY.
    SQLite + Postgres diverge on ordering without one, so delivery
    order could subtly change across backends. We now ORDER BY
    ``emitted_at, id`` — FIFO delivery within a batch. ``offset`` is
    exposed for callers that want to drain in pages; defaults preserve
    prior behaviour (first ``batch_size`` rows).
    """
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")

    sinks_tuple: tuple[Sink, ...] = tuple(sinks) if sinks is not None else DEFAULT_SINKS
    stats = DrainStats()
    now = datetime.now(timezone.utc)

    # 0.9-s R5: SKIP LOCKED closes the checkpoint load/upsert race. Without
    # it two workers running drain_once concurrently can both pull the same
    # EventOutbox row → call sinks twice → race the checkpoint upsert. With
    # FOR UPDATE SKIP LOCKED, PG locks each returned row for the caller's
    # transaction; a second concurrent worker silently skips already-held
    # rows. SQLAlchemy ignores the hint on SQLite (no real concurrency
    # anyway in the test env).
    pending_q = (
        select(EventOutbox)
        .where(EventOutbox.delivered_at.is_(None))
        .order_by(EventOutbox.emitted_at, EventOutbox.id)
        .offset(offset)
        .limit(batch_size)
    )
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        pending_q = pending_q.with_for_update(skip_locked=True)
    pending = db.execute(pending_q).scalars().all()

    for event in pending:
        stats.scanned += 1
        cp = _load_checkpoint(db, event.id)

        # Cooldown: respect retry_after. SQLite round-trips naive datetimes,
        # so coerce to aware UTC before comparison.
        if cp is not None and cp.retry_after is not None:
            ra = cp.retry_after
            if ra.tzinfo is None:
                ra = ra.replace(tzinfo=timezone.utc)
            if ra > now:
                stats.skipped += 1
                continue

        # Already-delivered idempotency guard: if checkpoint says delivered
        # but event_outbox.delivered_at is NULL (crash between writes), we
        # still re-run sinks — sinks must be idempotent per contract.

        failure: Exception | None = None
        for sink in sinks_tuple:
            try:
                sink(event)
            except Exception as exc:  # noqa: BLE001 — we log + record
                logger.exception(
                    "event_drain: sink %s raised on event %s (%s)",
                    getattr(sink, "__name__", repr(sink)),
                    event.id,
                    event.event_name,
                )
                failure = exc
                break  # stop calling further sinks for this event

        if failure is None:
            event.delivered_at = datetime.now(timezone.utc)
            _upsert_checkpoint(
                db,
                event.id,
                status="delivered",
                retry_count=cp.retry_count if cp else 0,
                last_error=None,
                retry_after=None,
            )
            stats.delivered += 1
        else:
            retry_count = (cp.retry_count if cp else 0) + 1
            status = "dead_letter" if retry_count > MAX_RETRIES else "retry"
            _upsert_checkpoint(
                db,
                event.id,
                status=status,
                retry_count=retry_count,
                last_error=f"{type(failure).__name__}: {failure}",
                retry_after=(
                    None if status == "dead_letter" else _next_retry_after(retry_count)
                ),
            )
            stats.retried += 1

    return stats
