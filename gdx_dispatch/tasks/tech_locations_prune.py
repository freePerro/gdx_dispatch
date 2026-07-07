"""Sprint 5 / S5-C5 — prune tech_location rows older than tenant retention.

Beat-scheduled task. Reads the per-tenant `tech_mobile.gps_retention_days`
setting (default 45) from app_settings and deletes breadcrumb rows older
than that.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import text

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal, app_engine
from gdx_dispatch.core.feature_defaults import TECH_MOBILE_SETTINGS

log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = TECH_MOBILE_SETTINGS["tech_mobile.gps_retention_days"]["default"]


@celery_app.task(name="gdx_dispatch.tasks.tech_locations_prune.prune_tech_locations_for_all_tenants", queue="priority:low")
def prune_tech_locations_for_all_tenants() -> dict[str, int]:
    """Drop breadcrumbs older than retention setting."""
    tenant_id = os.getenv("GDX_TENANT_ID") or os.getenv("GDX_DEFAULT_TENANT_ID") or "gdx"

    overrides: dict = {}
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT tenant_mobile_settings FROM app_settings LIMIT 1")
        ).mappings().first()
        overrides = (row["tenant_mobile_settings"] if row else None) or {}
    except Exception:
        log.exception("prune_failed reading app_settings tenant=%s", tenant_id)
    finally:
        db.close()

    days = overrides.get("tech_mobile.gps_retention_days") or DEFAULT_RETENTION_DAYS
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    days = max(7, min(int(days), 365))

    deleted_total = 0
    failures = 0
    try:
        with app_engine.begin() as conn:
            result = conn.execute(
                text(
                    "DELETE FROM tech_locations "
                    "WHERE recorded_at < NOW() - (:days || ' days')::interval"
                ),
                {"days": days},
            )
            deleted_total = result.rowcount or 0
        log.info("pruned tenant=%s days=%d deleted=%d", tenant_id, days, deleted_total)
    except Exception:
        log.exception("prune_failed tenant=%s", tenant_id)
        failures += 1

    return {"deleted": deleted_total, "failures": failures, "tenants": 1}
