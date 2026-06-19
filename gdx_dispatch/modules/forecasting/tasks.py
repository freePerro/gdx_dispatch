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
