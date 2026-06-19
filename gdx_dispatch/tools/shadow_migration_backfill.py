"""SS-29 slice D — shadow migration backfill CLI.

Batched, idempotent copy of existing rows from an old (v1) table into
its mapped new (v2) table. Supports resume via
``shadow_migration_checkpoint``.

Design rules (per SS-29 plan):

* **Idempotent.** Re-running from the same checkpoint produces the same
  result. Each batch uses UPSERT-by-pk semantics: the ``insert_row``
  callable MUST treat duplicate primary keys as no-ops (or updates that
  converge to the same state).
* **Resumable.** Before processing each batch, the current checkpoint is
  read; after each batch, the checkpoint is advanced and committed.
  A crash leaves the checkpoint at the last committed batch.
* **Progress reporting.** Every batch logs progress; ``--verbose``
  streams per-row. Final return code is 0 on success, non-zero if any
  batch errored (logger.exception + abort; partial checkpoint is
  preserved so a retry resumes).
* **No coupling to a specific v1 schema.** The CLI reads rows via a
  caller-provided ``fetch_batch`` callable — tests pass a stub; real
  production wiring will pass a SQL-reader over the old table.

Usage::

    python -m gdx_dispatch.tools.shadow_migration_backfill --tenant t1 --old-table customers_v1
    python -m gdx_dispatch.tools.shadow_migration_backfill --tenant t1 --old-table customers_v1 --reset

``--reset`` clears the checkpoint before starting.

TODO: real fetch/insert callables wire to the app's DB
session factory at main-chain merge.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from gdx_dispatch.core.shadow_schema_map import ShadowMap, shadow_for

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 500


@dataclass
class BackfillResult:
    tenant_id: str
    old_table: str
    new_table: str
    rows_processed: int = 0
    batches: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    resumed_from_pk: str | None = None
    error: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load_checkpoint(db: Any, tenant_id: str, old_table: str) -> Any:
    from gdx_dispatch.models.platform_ss29_additions import ShadowMigrationCheckpoint

    return (
        db.query(ShadowMigrationCheckpoint)
        .filter(
            ShadowMigrationCheckpoint.tenant_id == tenant_id,
            ShadowMigrationCheckpoint.old_table == old_table,
        )
        .first()
    )


def _save_checkpoint(
    db: Any,
    tenant_id: str,
    old_table: str,
    *,
    last_row_id: int | None,
    last_row_pk: str | None,
    row_count_this_session: int,
) -> None:
    from uuid import uuid4
    from gdx_dispatch.models.platform_ss29_additions import ShadowMigrationCheckpoint

    row = _load_checkpoint(db, tenant_id, old_table)
    if row is None:
        row = ShadowMigrationCheckpoint(
            id=uuid4(),
            tenant_id=tenant_id,
            old_table=old_table,
            last_row_id=last_row_id,
            last_row_pk=last_row_pk,
            row_count_this_session=row_count_this_session,
            updated_at=_utcnow(),
        )
        db.add(row)
    else:
        row.last_row_id = last_row_id
        row.last_row_pk = last_row_pk
        row.row_count_this_session = row_count_this_session
        row.updated_at = _utcnow()
    db.flush()


def reset_checkpoint(db: Any, tenant_id: str, old_table: str) -> None:
    """Delete the checkpoint row for this pair so the next run starts fresh."""
    row = _load_checkpoint(db, tenant_id, old_table)
    if row is not None:
        db.delete(row)
        db.flush()


def run_backfill(
    db: Any,
    *,
    tenant_id: str,
    old_table: str,
    fetch_batch: Callable[[str | None, int], Iterable[Mapping[str, Any]]],
    insert_row: Callable[[str, dict[str, Any]], None],
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_rows: int | None = None,
    sm: ShadowMap | None = None,
) -> BackfillResult:
    """Run the backfill loop until ``fetch_batch`` returns an empty batch
    or ``max_rows`` is reached.

    ``fetch_batch(last_pk, limit)`` must return rows ordered by the old
    table's primary key, *strictly greater than* ``last_pk`` (or all rows
    if ``last_pk is None``). Empty sequence ⇒ caught up.

    ``insert_row(new_table, row)`` performs the v2 insert; MUST be
    idempotent on PK collision.
    """
    if sm is None:
        sm = shadow_for(old_table)

    result = BackfillResult(
        tenant_id=tenant_id, old_table=old_table, new_table=sm.new_table,
    )

    cp = _load_checkpoint(db, tenant_id, old_table)
    last_pk: str | None = cp.last_row_pk if cp is not None else None
    if last_pk:
        result.resumed_from_pk = last_pk

    try:
        while True:
            rows = list(fetch_batch(last_pk, batch_size))
            if not rows:
                break

            for row in rows:
                pk_val = row.get(sm.primary_key)
                new_row = sm.transform_row(row)
                insert_row(sm.new_table, new_row)

                last_pk = str(pk_val) if pk_val is not None else last_pk
                result.rows_processed += 1

                if max_rows is not None and result.rows_processed >= max_rows:
                    break

            result.batches += 1
            _save_checkpoint(
                db, tenant_id, old_table,
                last_row_id=None,
                last_row_pk=last_pk,
                row_count_this_session=result.rows_processed,
            )
            db.commit()
            logger.info(
                "backfill: tenant=%s table=%s batch=%d total=%d last_pk=%s",
                tenant_id, old_table, result.batches,
                result.rows_processed, last_pk,
            )

            if max_rows is not None and result.rows_processed >= max_rows:
                break
    except Exception as exc:  # noqa: BLE001
        # Failure path: logger.exception + preserve checkpoint + propagate
        # as a non-zero exit in _main. Raising here keeps callers honest.
        result.error = f"{type(exc).__name__}: {exc}"
        logger.exception(
            "backfill: tenant=%s table=%s FAILED after batches=%d rows=%d: %s",
            tenant_id, old_table, result.batches, result.rows_processed, exc,
        )
        raise
    finally:
        result.finished_at = _utcnow()

    return result


def _main(argv: list[str] | None = None) -> int:  # pragma: no cover — CLI harness
    parser = argparse.ArgumentParser(description="SS-29 shadow migration backfill")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--old-table", required=True)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--reset", action="store_true",
                        help="Clear checkpoint before starting")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from gdx_dispatch.core.database import SessionLocal

    db = SessionLocal()
    try:
        if args.reset:
            reset_checkpoint(db, args.tenant, args.old_table)
            db.commit()

        def _fetch_unimplemented(last_pk, limit):
            raise NotImplementedError(
                "TODO: wire real fetch_batch in main chain"
            )

        def _insert_unimplemented(new_table, row):
            raise NotImplementedError(
                "TODO: wire real insert_row in main chain"
            )

        result = run_backfill(
            db,
            tenant_id=args.tenant,
            old_table=args.old_table,
            fetch_batch=_fetch_unimplemented,
            insert_row=_insert_unimplemented,
            batch_size=args.batch_size,
            max_rows=args.max_rows,
        )
    except Exception:
        return 2
    finally:
        db.close()

    print(
        f"backfill: tenant={result.tenant_id} table={result.old_table} "
        f"rows={result.rows_processed} batches={result.batches}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
