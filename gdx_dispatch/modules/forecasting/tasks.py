"""Celery tasks for forecasting.

Today: one nightly task — the observed-recurring detector. Beat schedule
is wired in ``gdx_dispatch/core/scheduler.py``.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)


def _tenant_session(tenant_id: str):
    """Open a session on the single application database."""
    return SessionLocal()


@celery_app.task(bind=True, max_retries=3, queue="priority:low")
def detect_observed_recurring_task(self, tenant_id: str) -> dict:
    """Run the observed-recurring detector against one tenant's DB.

    Skips silently if the tables don't exist yet (a tenant that hasn't
    received the slice-1 DDL via sync_tenant_db.py is just not eligible
    yet — no error needed).
    """
    from gdx_dispatch.modules.forecasting.observed_recurring import run_detector

    try:
        with _tenant_session(tenant_id) as db:
            # Defensive: if the slice-1 tables aren't present yet, skip.
            # Portable across SQLite + PG via SQLAlchemy inspection.
            if "recurring_streams" not in inspect(db.get_bind()).get_table_names():
                log.info("recurring_streams not present for tenant=%s — skipping detector", tenant_id)
                return {"tenant_id": tenant_id, "skipped": "schema_not_synced"}
            stats = run_detector(db)
            return {"tenant_id": tenant_id, **stats}
    except Exception as exc:
        log.exception("detect_observed_recurring_task failed for tenant=%s: %s", tenant_id, exc)
        raise self.retry(exc=exc, countdown=300)


@celery_app.task
def detect_observed_recurring_dispatcher() -> dict:
    """Beat-fired dispatcher: queues the detector for every tenant nightly."""
    queued: list[str] = []
    from gdx_dispatch.core.tenant import single_tenant
    t = single_tenant()
    try:
        detect_observed_recurring_task.delay(t["id"])
        queued.append(t["slug"])
    except Exception:
        log.exception("failed to queue detector for tenant=%s", t["slug"])
    return {"queued": len(queued), "slugs": queued}


@celery_app.task(bind=True, max_retries=3, queue="priority:low")
def advance_forecast_measurement_task(self, tenant_id: str) -> dict:
    """Advance the Stage-A measurement loop one day for a tenant:
    capture today's forecast snapshot, then reconcile any snapshots whose
    window has closed (docs/forecasting-accuracy-roadmap.md).

    Capture and reconcile are paired here because they're the two halves of one
    daily tick — freeze today's open AR, score yesterday's matured windows.
    Skips silently if the Stage-A tables aren't present yet (a tenant that
    hasn't received the DDL is just not eligible).
    """
    from gdx_dispatch.modules.forecasting import accuracy
    from gdx_dispatch.modules.forecasting.calibration import CALIBRATION_LOOKBACK_DAYS

    try:
        with _tenant_session(tenant_id) as db:
            if "forecast_snapshots" not in inspect(db.get_bind()).get_table_names():
                log.info("forecast_snapshots not present for tenant=%s — skipping measurement", tenant_id)
                return {"tenant_id": tenant_id, "skipped": "schema_not_synced"}
            snap = accuracy.capture_snapshot(db)
            reconciled = accuracy.reconcile_due_snapshots(db)
            # Keep the snapshot tables bounded (retention ≥ calibration lookback
            # so we never prune data calibration still reads).
            pruned = accuracy.prune_reconciled_snapshots(db, retention_days=CALIBRATION_LOOKBACK_DAYS)
            return {
                "tenant_id": tenant_id,
                "captured_snapshot_id": str(snap.id),
                "reconciled": len(reconciled),
                "pruned": pruned,
            }
    except Exception as exc:
        log.exception("advance_forecast_measurement_task failed for tenant=%s: %s", tenant_id, exc)
        raise self.retry(exc=exc, countdown=300)


@celery_app.task
def advance_forecast_measurement_dispatcher() -> dict:
    """Beat-fired dispatcher: queues the daily measurement tick for every tenant."""
    queued: list[str] = []
    from gdx_dispatch.core.tenant import single_tenant
    t = single_tenant()
    try:
        advance_forecast_measurement_task.delay(t["id"])
        queued.append(t["slug"])
    except Exception:
        log.exception("failed to queue forecast measurement for tenant=%s", t["slug"])
    return {"queued": len(queued), "slugs": queued}
