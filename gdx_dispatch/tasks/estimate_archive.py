"""Nightly archive of stale Draft estimates.

UX audit F-47 / 2026-04-29. Per-tenant policy `estimate_draft_archive_days`
in TenantSettings (default 60, 0 disables). Soft-deletes drafts whose
updated_at is older than that threshold. Logs per-tenant counts.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)


def _archive_for_tenant(tenant_id: str, threshold_days: int) -> int:
    """Soft-delete drafts older than threshold_days. Returns rows affected."""
    if threshold_days <= 0:
        return 0
    db = SessionLocal()
    archived = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=threshold_days)
        result = db.execute(
            text(
                """
                UPDATE estimates
                SET deleted_at = :now
                WHERE deleted_at IS NULL
                  AND status = 'draft'
                  AND COALESCE(updated_at, created_at) < :cutoff
                """
            ),
            {"now": datetime.now(timezone.utc), "cutoff": cutoff},
        )
        archived = result.rowcount or 0
        db.commit()
        if archived:
            log.info(
                "estimates_archived",
                extra={
                    "tenant_id": tenant_id,
                    "count": archived,
                    "threshold_days": threshold_days,
                },
            )
    except Exception:
        log.exception("estimate_archive_failed", extra={"tenant_id": tenant_id})
        db.rollback()
    finally:
        db.close()
    return archived


def _purge_empty_drafts_for_tenant(tenant_id: str, threshold_days: int) -> int:
    """Hard-delete drafts older than threshold_days that have zero lines and
    were never sent. Returns rows affected.

    Why hard-delete: an empty draft has nothing to recover. Soft-delete just
    leaves dead weight in the table the archive task already covers.
    """
    if threshold_days <= 0:
        return 0
    db = SessionLocal()
    purged = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=threshold_days)
        result = db.execute(
            text(
                """
                DELETE FROM estimates
                WHERE status = 'draft'
                  AND sent_at IS NULL
                  AND created_at < :cutoff
                  AND NOT EXISTS (
                      SELECT 1 FROM estimate_lines el
                      WHERE el.estimate_id = estimates.id
                  )
                """
            ),
            {"cutoff": cutoff},
        )
        purged = result.rowcount or 0
        db.commit()
        if purged:
            log.info(
                "empty_draft_estimates_purged",
                extra={
                    "tenant_id": tenant_id,
                    "count": purged,
                    "threshold_days": threshold_days,
                },
            )
    except Exception:
        log.exception("estimate_empty_draft_purge_failed", extra={"tenant_id": tenant_id})
        db.rollback()
    finally:
        db.close()
    return purged


@celery_app.task(name="estimates.purge_empty_drafts_for_all_tenants", queue="priority:low")
def purge_empty_drafts_for_all_tenants() -> dict:
    """Hard-delete empty drafts older than 7 days.

    Threshold is fixed at 7 days (not configurable). The whole point of an
    empty draft is that it represents an abandoned form session; keeping it
    longer than a week serves no one.
    """
    THRESHOLD_DAYS = 7
    tenant_id = os.getenv("GDX_TENANT_ID") or os.getenv("GDX_DEFAULT_TENANT_ID") or "gdx"
    total = 0
    try:
        total += _purge_empty_drafts_for_tenant(tenant_id, THRESHOLD_DAYS)
    except Exception:
        log.exception("purge_empty_drafts_for_all_tenants_failed")
    return {"tenants_checked": 1, "estimates_purged": total}


@celery_app.task(name="estimates.archive_stale_drafts_for_all_tenants", queue="priority:low")
def archive_stale_drafts_for_all_tenants() -> dict:
    """Soft-delete stale drafts, honoring the per-tenant archive threshold."""
    tenant_id = os.getenv("GDX_TENANT_ID") or os.getenv("GDX_DEFAULT_TENANT_ID") or "gdx"
    total = 0
    try:
        db = SessionLocal()
        try:
            days_row = db.execute(
                text("SELECT estimate_draft_archive_days FROM tenant_settings LIMIT 1")
            ).mappings().first()
            days = int((days_row["estimate_draft_archive_days"] if days_row else None) or 60)
        finally:
            db.close()
        total += _archive_for_tenant(tenant_id, days)
    except Exception:
        log.exception("archive_stale_drafts_for_all_tenants_failed")
    return {"tenants_checked": 1, "estimates_archived": total}
