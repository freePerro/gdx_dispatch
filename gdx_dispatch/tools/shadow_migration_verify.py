"""SS-29 slice E — shadow migration verify CLI.

Random-sample comparison of old-vs-new rows for a (tenant, old_table)
pair. Emits a structured drift report and exits non-zero if any drift
is detected.

Rules:

* **Sample-based** — real v1 tables can have millions of rows. Default
  sample size is 1000 rows; override via ``--sample``.
* **Canonical-JSON sha256** comparison (same function as the ShadowWriter
  drift detector) so the verifier agrees byte-for-byte with dual-write.
* **Non-zero exit on drift** — intended for CI/cron use.
* **No destructive ops.** Verify is read-only; any mismatch it finds is
  written to ``shadow_migration_drift`` as a ``reason="hash_mismatch"``
  row (so the admin UI surfaces it the same as live drift).

Usage::

    python -m gdx_dispatch.tools.shadow_migration_verify --tenant t1 --old-table customers_v1
    python -m gdx_dispatch.tools.shadow_migration_verify --tenant t1 --old-table customers_v1 --sample 500
"""
from __future__ import annotations

import argparse
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from gdx_dispatch.core.shadow_migration import row_fingerprint
from gdx_dispatch.core.shadow_schema_map import ShadowMap, shadow_for

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_SIZE = 1000


@dataclass
class DriftRecord:
    pk: Any
    reason: str
    old_hash: str | None
    new_hash: str | None


@dataclass
class VerifyResult:
    tenant_id: str
    old_table: str
    new_table: str
    sampled: int = 0
    drift_count: int = 0
    drifts: list[DriftRecord] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.drift_count == 0


def run_verify(
    db: Any,
    *,
    tenant_id: str,
    old_table: str,
    sample_old: Callable[[int], Iterable[Mapping[str, Any]]],
    read_new_row: Callable[[str, str, Any], dict[str, Any] | None],
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    record_drift: bool = True,
    sm: ShadowMap | None = None,
) -> VerifyResult:
    """Compare up to ``sample_size`` old rows against the new table.

    ``sample_old(n)`` returns at most ``n`` randomly-chosen rows from
    the old table. The verifier is callable-driven so tests can pass an
    in-memory population; production wiring will use a TABLESAMPLE
    statement or ``ORDER BY RANDOM() LIMIT``.

    Drift rows are persisted to ``shadow_migration_drift`` when
    ``record_drift=True``.
    """
    if sm is None:
        sm = shadow_for(old_table)

    result = VerifyResult(
        tenant_id=tenant_id, old_table=old_table, new_table=sm.new_table,
    )
    new_pk_col = sm.column_renames.get(sm.primary_key, sm.primary_key)

    for old_row in sample_old(sample_size):
        result.sampled += 1
        expected = sm.transform_row(old_row)
        pk_value = expected.get(new_pk_col)
        old_hash = row_fingerprint(expected)

        stored = read_new_row(sm.new_table, new_pk_col, pk_value) if pk_value is not None else None
        if stored is None:
            result.drift_count += 1
            result.drifts.append(DriftRecord(
                pk=pk_value, reason="new_row_missing",
                old_hash=old_hash, new_hash=None,
            ))
            if record_drift:
                _persist_drift(db, tenant_id, old_table,
                              reason="new_row_missing",
                              old_hash=old_hash, new_hash=None,
                              pk_value=pk_value, new_pk_col=new_pk_col)
            continue

        new_hash = row_fingerprint(stored)
        if old_hash != new_hash:
            result.drift_count += 1
            result.drifts.append(DriftRecord(
                pk=pk_value, reason="hash_mismatch",
                old_hash=old_hash, new_hash=new_hash,
            ))
            if record_drift:
                _persist_drift(db, tenant_id, old_table,
                              reason="hash_mismatch",
                              old_hash=old_hash, new_hash=new_hash,
                              pk_value=pk_value, new_pk_col=new_pk_col)

    result.finished_at = datetime.now(timezone.utc)
    logger.info(
        "verify: tenant=%s table=%s sampled=%d drift=%d",
        tenant_id, old_table, result.sampled, result.drift_count,
    )
    return result


def _persist_drift(
    db: Any,
    tenant_id: str,
    old_table: str,
    *,
    reason: str,
    old_hash: str | None,
    new_hash: str | None,
    pk_value: Any,
    new_pk_col: str,
) -> None:
    try:
        from gdx_dispatch.models.platform_ss29_additions import ShadowMigrationDrift

        row = ShadowMigrationDrift(
            id=uuid4(),
            tenant_id=tenant_id,
            old_table=old_table,
            reason=reason,
            old_hash=old_hash,
            new_hash=new_hash,
            details={"pk": new_pk_col, "pk_value": str(pk_value),
                     "source": "verify"},
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.flush()
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "verify: drift-row persist failed tenant=%s table=%s err=%s",
            tenant_id, old_table, exc,
        )


def sample_old_rows_from_list(source: list[Mapping[str, Any]], seed: int | None = None):
    """Testing helper: return a callable that samples ``source``."""
    rnd = random.Random(seed)

    def sampler(n: int):
        if n >= len(source):
            return list(source)
        return rnd.sample(source, n)

    return sampler


def _main(argv: list[str] | None = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description="SS-29 shadow migration verify")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--old-table", required=True)
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--no-record", action="store_true",
                        help="Do not persist drift rows (dry run)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    from gdx_dispatch.core.database import SessionLocal

    db = SessionLocal()
    try:
        def _sample_unimpl(n):
            raise NotImplementedError(
                "TODO: wire real sample_old in main chain"
            )

        def _read_unimpl(nt, pk_col, pk):
            raise NotImplementedError(
                "TODO: wire real read_new_row in main chain"
            )

        result = run_verify(
            db,
            tenant_id=args.tenant,
            old_table=args.old_table,
            sample_old=_sample_unimpl,
            read_new_row=_read_unimpl,
            sample_size=args.sample,
            record_drift=not args.no_record,
        )
    finally:
        db.close()

    print(
        f"verify: sampled={result.sampled} drift={result.drift_count} "
        f"ok={result.ok}"
    )
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
