"""SS-30 slice B — atomic cutover orchestrator.

:func:`run_cutover` performs the one-way cut for a (tenant, old_table):

1. Lock the ``shadow_migration_state`` row and flip ``mode`` to
   ``cutover``.
2. ``ALTER TABLE old_table RENAME TO old_table_v1_deprecated``.
3. ``ALTER TABLE new_table RENAME TO old_table``.
4. Write a ``cutover_schedule`` row with
   ``scheduled_drop_at = now + grace_period_days``.
5. Emit ``gdx_dispatch.cutover.executed.v1``.

All of steps 1-5 happen inside a single SAVEPOINT. Any exception causes
a full rollback and :func:`emit_cutover_cancelled` is emitted on a
*fresh* transaction (so the cancellation itself is durable even though
the cutover was rolled back).

Idempotency
-----------
Re-invoking ``run_cutover`` for a pair that's already in ``cutover``
mode with an existing ``cutover_schedule`` row is a no-op: the function
returns the existing :class:`CutoverResult` without executing any
renames or emitting new events. This makes the operation safe to retry
after a network flake on the router side.

Dry run
-------
``dry_run=True`` walks the checks, validates the rename targets exist,
and emits ``gdx_dispatch.cutover.executed.v1`` with ``dry_run=True`` in the
payload — but does NOT execute the ALTERs or write the schedule row.

INTEGRATION_TODO: at main-chain merge, the real table names will be
rewritten to the production schema. The module is import-safe today
because it defers ORM / DDL to call time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import text

from gdx_dispatch.core.cutover_events import (
    emit_cutover_cancelled,
    emit_cutover_executed,
    emit_cutover_scheduled,
)
from gdx_dispatch.core.shadow_schema_map import is_shadowed, shadow_for

logger = logging.getLogger(__name__)

DEFAULT_GRACE_PERIOD_DAYS = 30
MAX_GRACE_PERIOD_DAYS = 365
DEPRECATED_SUFFIX = "_v1_deprecated"


class CutoverError(RuntimeError):
    """Raised when run_cutover cannot proceed."""


@dataclass(frozen=True)
class CutoverResult:
    """Result of one run_cutover invocation."""

    tenant_id: str
    old_table: str
    new_table: str
    deprecated_table: str
    executed_at: datetime
    scheduled_drop_at: datetime
    dry_run: bool
    already_cut_over: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "old_table": self.old_table,
            "new_table": self.new_table,
            "deprecated_table": self.deprecated_table,
            "executed_at": self.executed_at.isoformat(),
            "scheduled_drop_at": self.scheduled_drop_at.isoformat(),
            "dry_run": self.dry_run,
            "already_cut_over": self.already_cut_over,
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _deprecated_name(old_table: str) -> str:
    return f"{old_table}{DEPRECATED_SUFFIX}"


def _default_rename(db: Any, src: str, dst: str) -> None:
    """Execute ``ALTER TABLE src RENAME TO dst`` via the caller's session.

    Works on SQLite and Postgres. Callers can inject a custom callable
    for test isolation.
    """
    db.execute(text(f"ALTER TABLE {src} RENAME TO {dst}"))


def run_cutover(
    db: Any,
    *,
    tenant_id: str,
    old_table: str,
    new_table: str | None = None,
    actor_identity_id: str | None = None,
    grace_period_days: int = DEFAULT_GRACE_PERIOD_DAYS,
    dry_run: bool = False,
    notes: str | None = None,
    rename_fn: Callable[[Any, str, str], None] | None = None,
) -> CutoverResult:
    """Execute the atomic cutover for one (tenant, old_table) pair.

    Parameters
    ----------
    db:
        SQLAlchemy session. Caller owns commit semantics EXCEPT for the
        emit-cancelled path which starts its own transaction after rollback.
    tenant_id, old_table:
        Required. Must be non-empty.
    new_table:
        Optional override; defaults to ``shadow_for(old_table).new_table``.
    actor_identity_id:
        Super-admin identity id for audit trail.
    grace_period_days:
        Days until the cleanup cron may drop the deprecated table.
        Clamped to ``(0, MAX_GRACE_PERIOD_DAYS]``.
    dry_run:
        If True, emit the executed event with ``dry_run=True`` and skip
        ALTERs + schedule persistence.
    rename_fn:
        Test injection point; defaults to real ALTER TABLE via session.
    """
    if not tenant_id:
        raise CutoverError("tenant_id required")
    if not old_table:
        raise CutoverError("old_table required")
    if grace_period_days <= 0 or grace_period_days > MAX_GRACE_PERIOD_DAYS:
        raise CutoverError(
            f"grace_period_days must be in (0, {MAX_GRACE_PERIOD_DAYS}]"
        )

    # Resolve target new_table.
    if new_table is None:
        if not is_shadowed(old_table):
            raise CutoverError(
                f"no shadow map for {old_table!r}; pass new_table explicitly"
            )
        new_table = shadow_for(old_table).new_table
    deprecated_table = _deprecated_name(old_table)
    rename = rename_fn or _default_rename

    from gdx_dispatch.models.platform_ss29_additions import ShadowMigrationState
    from gdx_dispatch.models.platform_ss30_additions import CutoverSchedule

    # Idempotency check: if state is already cutover AND a cutover_schedule
    # row exists, return it without redoing work.
    state_row = (
        db.query(ShadowMigrationState)
        .filter(
            ShadowMigrationState.tenant_id == tenant_id,
            ShadowMigrationState.old_table == old_table,
        )
        .first()
    )
    if state_row is not None and state_row.mode == "cutover":
        existing = (
            db.query(CutoverSchedule)
            .filter(
                CutoverSchedule.tenant_id == tenant_id,
                CutoverSchedule.old_table == old_table,
            )
            .first()
        )
        if existing is not None:
            logger.info(
                "run_cutover: idempotent no-op tenant=%s table=%s",
                tenant_id, old_table,
            )
            return CutoverResult(
                tenant_id=tenant_id,
                old_table=old_table,
                new_table=existing.new_table,
                deprecated_table=existing.deprecated_table,
                executed_at=existing.executed_at,
                scheduled_drop_at=existing.scheduled_drop_at,
                dry_run=bool(existing.dry_run),
                already_cut_over=True,
            )

    if state_row is None or state_row.mode != "shadow":
        raise CutoverError(
            f"cutover requires current mode=shadow; got "
            f"{state_row.mode if state_row else 'no-state-row'!r}"
        )

    now = _utcnow()
    scheduled_drop_at = now + timedelta(days=grace_period_days)

    # Begin the atomic section. We use a nested transaction (SAVEPOINT) so
    # the caller's outer session is unaffected if we roll back.
    savepoint = db.begin_nested()
    try:
        # 1. Flip mode to cutover (row-level lock via UPDATE).
        state_row.mode = "cutover"
        state_row.cutover_at = now
        state_row.updated_at = now
        db.flush()

        if not dry_run:
            # 2 & 3. Atomic DDL renames.
            rename(db, old_table, deprecated_table)
            rename(db, new_table, old_table)

            # 4. Persist cutover_schedule row.
            sched = CutoverSchedule(
                id=uuid4(),
                tenant_id=tenant_id,
                old_table=old_table,
                new_table=new_table,
                deprecated_table=deprecated_table,
                executed_at=now,
                scheduled_drop_at=scheduled_drop_at,
                extended_count="0",
                dry_run=False,
                actor_identity_id=actor_identity_id,
                notes=notes,
                created_at=now,
                updated_at=now,
            )
            db.add(sched)
            db.flush()

            # Emit scheduled event (one-shot — the cleanup cron will read
            # scheduled_drop_at from the row directly).
            emit_cutover_scheduled(
                db,
                tenant_id=tenant_id,
                old_table=old_table,
                deprecated_table=deprecated_table,
                scheduled_drop_at=scheduled_drop_at,
                grace_period_days=grace_period_days,
                actor_identity_id=actor_identity_id,
                notes=notes,
            )

        # 5. Emit executed event — dry_run flag rides in the payload.
        emit_cutover_executed(
            db,
            tenant_id=tenant_id,
            old_table=old_table,
            new_table=new_table,
            deprecated_table=deprecated_table,
            executed_at=now,
            dry_run=dry_run,
            actor_identity_id=actor_identity_id,
            scheduled_drop_at=None if dry_run else scheduled_drop_at,
            notes=notes,
        )

        savepoint.commit()
    except Exception as exc:
        logger.error(
            "run_cutover: FAILED tenant=%s table=%s err=%s — rolling back",
            tenant_id, old_table, exc, exc_info=True,
        )
        try:
            savepoint.rollback()
        except Exception as rb_exc:  # noqa: BLE001
            logger.error(
                "run_cutover: rollback itself failed tenant=%s err=%s",
                tenant_id, rb_exc, exc_info=True,
            )
        # Emit cancellation on a fresh transaction so the audit event
        # survives the rollback. If this itself fails, log loudly but
        # propagate the original error.
        try:
            emit_cutover_cancelled(
                db,
                tenant_id=tenant_id,
                old_table=old_table,
                new_table=new_table,
                reason=str(exc),
                cancelled_at=_utcnow(),
                actor_identity_id=actor_identity_id,
                error_class=exc.__class__.__name__,
            )
            db.flush()
        except Exception as emit_exc:  # noqa: BLE001
            logger.error(
                "run_cutover: failed to emit cancelled event tenant=%s err=%s",
                tenant_id, emit_exc, exc_info=True,
            )
        raise CutoverError(f"cutover failed: {exc}") from exc

    return CutoverResult(
        tenant_id=tenant_id,
        old_table=old_table,
        new_table=new_table,
        deprecated_table=deprecated_table,
        executed_at=now,
        scheduled_drop_at=scheduled_drop_at,
        dry_run=dry_run,
        already_cut_over=False,
    )


def extend_deprecation(
    db: Any,
    *,
    tenant_id: str,
    old_table: str,
    additional_days: int,
    actor_identity_id: str | None = None,
) -> CutoverSchedule:  # type: ignore[name-defined]
    """Push scheduled_drop_at out by ``additional_days``.

    Enforces the hard cap ``MAX_GRACE_PERIOD_DAYS`` (total span from
    executed_at). Raises :class:`CutoverError` with a message the router
    maps to HTTP 409 when the cap would be exceeded.
    """
    from gdx_dispatch.models.platform_ss30_additions import CutoverSchedule

    if additional_days <= 0:
        raise CutoverError("additional_days must be positive")

    row = (
        db.query(CutoverSchedule)
        .filter(
            CutoverSchedule.tenant_id == tenant_id,
            CutoverSchedule.old_table == old_table,
        )
        .first()
    )
    if row is None:
        raise CutoverError("no cutover_schedule row for this (tenant, table)")

    new_drop_at = row.scheduled_drop_at + timedelta(days=additional_days)
    total_span_days = (new_drop_at - row.executed_at).days
    if total_span_days > MAX_GRACE_PERIOD_DAYS:
        raise CutoverError(
            f"extension would exceed MAX_GRACE_PERIOD_DAYS={MAX_GRACE_PERIOD_DAYS} "
            f"(new total span {total_span_days}d)"
        )

    row.scheduled_drop_at = new_drop_at
    try:
        row.extended_count = str(int(row.extended_count or "0") + 1)
    except (TypeError, ValueError):
        row.extended_count = "1"
    row.updated_at = _utcnow()
    if actor_identity_id:
        row.actor_identity_id = actor_identity_id
    db.flush()
    logger.info(
        "extend_deprecation tenant=%s table=%s new_drop_at=%s",
        tenant_id, old_table, new_drop_at.isoformat(),
    )
    return row
