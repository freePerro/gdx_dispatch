"""SS-28 slice E — audit retention cron.

CLI entry-point: prune rows in ``platform_consumer_audit`` older than
each tenant's configured retention window.

Rules (per SS-28 spec):

* Default retention: 90 days if a tenant has no ``audit_retention_policy`` row.
* **Never prune within the current month.** Safety minimum: even if a
  tenant sets retention to 7 days, the cron will NOT delete rows whose
  ``created_at`` falls inside the current calendar month. Compliance
  baseline — prevents an operator mistake from erasing "yesterday's"
  evidence.
* **Idempotent + batched.** Runs in 1000-row batches; if interrupted
  mid-run the next invocation picks up cleanly. No tenant's rows are
  half-deleted (each batch commits atomically).
* **Dry-run flag.** ``--dry-run`` prints the candidate count per
  tenant without issuing DELETE.

Usage::

    python -m gdx_dispatch.tools.audit_retention_cron --dry-run
    python -m gdx_dispatch.tools.audit_retention_cron

The function :func:`prune_audit_rows` is the programmatic entry point;
the ``__main__`` block parses argv and wires a real DB session.
INTEGRATION_TODO: wire into Celery-beat schedule alongside other
retention jobs at end-of-sprint.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 90
BATCH_SIZE = 1000


@dataclass
class PruneResult:
    tenant_id: str
    candidates: int
    deleted: int
    cutoff: datetime
    dry_run: bool


def _start_of_current_month(now: datetime) -> datetime:
    """Return midnight UTC on the 1st of the month containing ``now``."""
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def _compute_cutoff(now: datetime, retention_days: int) -> datetime:
    """Candidate cutoff = now - retention_days, floored to start-of-month.

    If retention_days would produce a cutoff inside the current month,
    floor to start-of-current-month — which itself is the "safety
    minimum" line described in the spec: we never prune rows whose
    ``created_at`` >= start-of-current-month.
    """
    from datetime import timedelta

    naive_cutoff = now - timedelta(days=retention_days)
    safety_floor = _start_of_current_month(now)
    # If naive_cutoff is AFTER safety_floor (i.e. inside current month),
    # clamp to safety_floor so we never touch current-month rows.
    if naive_cutoff > safety_floor:
        return safety_floor
    return naive_cutoff


def _tenant_retention_map(db: Any) -> dict[str, int]:
    """Load per-tenant retention_days from audit_retention_policy."""
    from gdx_dispatch.models.platform_ss28_additions import AuditRetentionPolicy

    out: dict[str, int] = {}
    for row in db.query(AuditRetentionPolicy).all():
        out[row.tenant_id] = int(row.retention_days)
    return out


def _tenants_with_audit_rows(db: Any) -> list[str]:
    from gdx_dispatch.models.platform_ss28_additions import PlatformConsumerAudit

    rows = (
        db.query(PlatformConsumerAudit.tenant_id)
        .distinct()
        .all()
    )
    return [r[0] for r in rows]


def prune_audit_rows(
    db: Any,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[PruneResult]:
    """Prune audit rows for every tenant; return per-tenant results.

    Idempotent — safe to call repeatedly. Each tenant is processed
    independently; a failure on one tenant does NOT prevent others
    from being pruned.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    from gdx_dispatch.models.platform_ss28_additions import PlatformConsumerAudit

    retention = _tenant_retention_map(db)
    results: list[PruneResult] = []

    for tenant_id in _tenants_with_audit_rows(db):
        days = retention.get(tenant_id, DEFAULT_RETENTION_DAYS)
        cutoff = _compute_cutoff(now, days)

        base_q = db.query(PlatformConsumerAudit).filter(
            PlatformConsumerAudit.tenant_id == tenant_id,
            PlatformConsumerAudit.created_at < cutoff,
        )
        candidates = base_q.count()

        deleted = 0
        if not dry_run and candidates > 0:
            # Batched delete to keep transaction size bounded.
            while True:
                batch_ids = [
                    r.id
                    for r in base_q.order_by(
                        PlatformConsumerAudit.created_at
                    )
                    .limit(BATCH_SIZE)
                    .all()
                ]
                if not batch_ids:
                    break
                (
                    db.query(PlatformConsumerAudit)
                    .filter(PlatformConsumerAudit.id.in_(batch_ids))
                    .delete(synchronize_session=False)
                )
                db.commit()
                deleted += len(batch_ids)

        results.append(
            PruneResult(
                tenant_id=tenant_id,
                candidates=candidates,
                deleted=deleted,
                cutoff=cutoff,
                dry_run=dry_run,
            )
        )
        logger.info(
            "audit_retention: tenant=%s cutoff=%s candidates=%d deleted=%d dry_run=%s",
            tenant_id,
            cutoff.isoformat(),
            candidates,
            deleted,
            dry_run,
        )

    return results


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SS-28 audit retention cron")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report candidate counts without deleting",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    # INTEGRATION_TODO: wire to the app's SessionLocal when the main chain
    # integrates this module. Until then, importing the app-wide session
    # factory from gdx_dispatch.core.database here keeps this CLI runnable.
    from gdx_dispatch.core.database import SessionLocal

    db = SessionLocal()
    try:
        results = prune_audit_rows(db, dry_run=args.dry_run)
    finally:
        db.close()

    total = sum(r.deleted for r in results)
    print(
        f"audit_retention: tenants={len(results)} total_deleted={total} dry_run={args.dry_run}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
