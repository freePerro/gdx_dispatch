"""SS-30 slice E — post-cutover cleanup cron.

Drops ``*_v1_deprecated`` tables whose ``scheduled_drop_at`` has elapsed.
Intended to run as a cron job (e.g., daily at 02:00 UTC).

Hard safety rules
-----------------

* **NEVER drop a table whose ``scheduled_drop_at`` has not elapsed.**
  The ``_is_ready_to_drop`` check is the final guard — regardless of
  how the caller filters rows, every row is re-checked at drop time.
* **``--confirm`` required for real drops.** Without it, the CLI runs
  in dry-run mode: logs what it *would* do, emits
  ``gdx_dispatch.cutover.deprecated_table_dropped.v1`` with ``dry_run=True``,
  but never executes DROP TABLE.
* **Per-row transaction.** Each DROP + event + schedule-update happens
  in its own transaction so a single failure does not block the rest.

Usage::

    # Preview:
    python -m gdx_dispatch.tools.cutover_cleanup_cron --dry-run

    # Real drops (DESTRUCTIVE):
    python -m gdx_dispatch.tools.cutover_cleanup_cron --confirm

Optional filters: ``--tenant``, ``--old-table``, ``--limit``.

TODO: at main-chain merge, the ``session_factory`` default
will be set to the app's real factory. Until then, callers must pass
one explicitly.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import text

from gdx_dispatch.core.cutover_events import emit_deprecated_table_dropped

logger = logging.getLogger(__name__)


@dataclass
class CleanupRow:
    """One schedule row evaluated by the cron."""

    tenant_id: str
    old_table: str
    deprecated_table: str
    scheduled_drop_at: datetime
    dropped_at: datetime | None = None
    action: str = "skipped"
    reason: str = ""


@dataclass
class CleanupResult:
    """Aggregate result of one cron run."""

    dry_run: bool
    rows: list[CleanupRow] = field(default_factory=list)
    ran_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def dropped_count(self) -> int:
        return sum(1 for r in self.rows if r.action == "dropped")

    @property
    def would_drop_count(self) -> int:
        return sum(1 for r in self.rows if r.action == "would_drop")

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.rows if r.action == "skipped")

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "ran_at": self.ran_at.isoformat(),
            "dropped": self.dropped_count,
            "would_drop": self.would_drop_count,
            "skipped": self.skipped_count,
            "rows": [
                {
                    "tenant_id": r.tenant_id,
                    "old_table": r.old_table,
                    "deprecated_table": r.deprecated_table,
                    "scheduled_drop_at": r.scheduled_drop_at.isoformat(),
                    "dropped_at": (
                        r.dropped_at.isoformat() if r.dropped_at else None
                    ),
                    "action": r.action,
                    "reason": r.reason,
                }
                for r in self.rows
            ],
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_ready_to_drop(
    scheduled_drop_at: datetime, now: datetime
) -> tuple[bool, str]:
    """Hard guard: only drop if scheduled_drop_at has elapsed."""
    sched = scheduled_drop_at
    if sched.tzinfo is None:
        sched = sched.replace(tzinfo=timezone.utc)
    if now < sched:
        return False, (
            f"scheduled_drop_at={sched.isoformat()} > now={now.isoformat()}"
        )
    return True, "ready"


def _default_drop(db: Any, deprecated_table: str) -> None:
    """Execute ``DROP TABLE deprecated_table``.

    Uses IF EXISTS so a re-run after a half-completed drop is a no-op.
    """
    db.execute(text(f"DROP TABLE IF EXISTS {deprecated_table}"))


def _default_record(
    db: Any,
    *,
    tenant_id: str,
    old_table: str | None,
    deprecated_table: str,
    scheduled_drop_at: datetime,
    dropped_at: datetime,
    dry_run: bool,
    actor_identity_id: str | None,
) -> None:
    """Append to :class:`DeprecatedTableRecord`."""
    from gdx_dispatch.models.platform_ss30_additions import DeprecatedTableRecord

    row = DeprecatedTableRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        old_table=old_table,
        deprecated_table=deprecated_table,
        scheduled_drop_at=scheduled_drop_at,
        dropped_at=dropped_at,
        dry_run=dry_run,
        actor_identity_id=actor_identity_id,
        created_at=dropped_at,
    )
    db.add(row)
    db.flush()


def run_cleanup(
    db: Any,
    *,
    confirm: bool = False,
    tenant_filter: str | None = None,
    table_filter: str | None = None,
    limit: int | None = None,
    actor_identity_id: str | None = None,
    now: datetime | None = None,
    drop_fn: Callable[[Any, str], None] | None = None,
    record_fn: Callable[..., None] | None = None,
) -> CleanupResult:
    """Run one cleanup pass.

    If ``confirm`` is False the run is dry — no DROP, no record row.
    The hard guard ``_is_ready_to_drop`` still fires regardless.
    """
    from gdx_dispatch.models.platform_ss30_additions import CutoverSchedule

    now = now or _utcnow()
    drop_fn = drop_fn or _default_drop
    record_fn = record_fn or _default_record
    dry_run = not confirm

    q = db.query(CutoverSchedule).filter(CutoverSchedule.dropped_at.is_(None))
    if tenant_filter:
        q = q.filter(CutoverSchedule.tenant_id == tenant_filter)
    if table_filter:
        q = q.filter(CutoverSchedule.old_table == table_filter)
    q = q.order_by(CutoverSchedule.scheduled_drop_at)
    if limit:
        q = q.limit(int(limit))
    rows = q.all()

    result = CleanupResult(dry_run=dry_run, ran_at=now)

    for row in rows:
        ready, detail = _is_ready_to_drop(row.scheduled_drop_at, now)
        entry = CleanupRow(
            tenant_id=row.tenant_id,
            old_table=row.old_table,
            deprecated_table=row.deprecated_table,
            scheduled_drop_at=row.scheduled_drop_at,
            reason=detail,
        )
        if not ready:
            entry.action = "skipped"
            result.rows.append(entry)
            logger.info(
                "cleanup_cron: SKIP tenant=%s table=%s — %s",
                row.tenant_id, row.deprecated_table, detail,
            )
            continue

        if dry_run:
            entry.action = "would_drop"
            entry.dropped_at = None
            result.rows.append(entry)
            # Still emit event with dry_run=True for audit.
            try:
                emit_deprecated_table_dropped(
                    db,
                    tenant_id=row.tenant_id,
                    deprecated_table=row.deprecated_table,
                    scheduled_drop_at=row.scheduled_drop_at,
                    old_table=row.old_table,
                    dropped_at=now,
                    dry_run=True,
                    actor_identity_id=actor_identity_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "cleanup_cron: dry-run event emit failed tenant=%s err=%s",
                    row.tenant_id, exc, exc_info=True,
                )
            logger.info(
                "cleanup_cron: WOULD DROP tenant=%s table=%s",
                row.tenant_id, row.deprecated_table,
            )
            continue

        # Real drop path — per-row transaction.
        try:
            drop_fn(db, row.deprecated_table)
            dropped_at = _utcnow()
            row.dropped_at = dropped_at
            row.updated_at = dropped_at
            db.flush()

            record_fn(
                db,
                tenant_id=row.tenant_id,
                old_table=row.old_table,
                deprecated_table=row.deprecated_table,
                scheduled_drop_at=row.scheduled_drop_at,
                dropped_at=dropped_at,
                dry_run=False,
                actor_identity_id=actor_identity_id,
            )
            emit_deprecated_table_dropped(
                db,
                tenant_id=row.tenant_id,
                deprecated_table=row.deprecated_table,
                scheduled_drop_at=row.scheduled_drop_at,
                old_table=row.old_table,
                dropped_at=dropped_at,
                dry_run=False,
                actor_identity_id=actor_identity_id,
            )
            db.commit()
            entry.action = "dropped"
            entry.dropped_at = dropped_at
            result.rows.append(entry)
            logger.warning(
                "cleanup_cron: DROPPED tenant=%s table=%s",
                row.tenant_id, row.deprecated_table,
            )
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            entry.action = "skipped"
            entry.reason = f"drop failed: {exc}"
            result.rows.append(entry)
            logger.error(
                "cleanup_cron: DROP FAILED tenant=%s table=%s err=%s",
                row.tenant_id, row.deprecated_table, exc, exc_info=True,
            )

    logger.info(
        "cleanup_cron: done dry_run=%s dropped=%d would_drop=%d skipped=%d",
        dry_run, result.dropped_count, result.would_drop_count, result.skipped_count,
    )
    return result


# ----------------------------------------------------------------------
# CLI entrypoint
# ----------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="gdx_dispatch.tools.cutover_cleanup_cron",
        description=(
            "Drop *_v1_deprecated tables past their grace period. "
            "Defaults to dry-run; pass --confirm for destructive drops."
        ),
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="DESTRUCTIVE: actually execute DROP TABLE. Without this "
             "flag the tool runs in dry-run mode.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run flag (default behavior; ignored if --confirm set).",
    )
    p.add_argument("--tenant", default=None, help="Filter to one tenant id")
    p.add_argument("--old-table", default=None, help="Filter to one old_table")
    p.add_argument("--limit", type=int, default=None, help="Max rows this run")
    p.add_argument("--actor", default="cron", help="actor_identity_id for audit")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> int:
    args = _parse_args(list(argv if argv is not None else sys.argv[1:]))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.confirm and args.dry_run:
        logger.warning(
            "cleanup_cron: both --confirm and --dry-run passed; --confirm wins"
        )
    confirm = bool(args.confirm)
    if not confirm:
        logger.info("cleanup_cron: DRY-RUN mode (pass --confirm for real drops)")

    if session_factory is None:
        logger.error(
            "cleanup_cron: no session_factory supplied — TODO "
            "wire app.db.session_factory at main-chain merge"
        )
        return 2

    db = session_factory()
    try:
        result = run_cleanup(
            db,
            confirm=confirm,
            tenant_filter=args.tenant,
            table_filter=args.old_table,
            limit=args.limit,
            actor_identity_id=args.actor,
        )
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

    # Final summary.
    logger.info(
        "cleanup_cron: SUMMARY dry_run=%s dropped=%d would_drop=%d skipped=%d",
        result.dry_run,
        result.dropped_count,
        result.would_drop_count,
        result.skipped_count,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
