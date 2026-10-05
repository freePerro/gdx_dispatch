"""Nightly archive of stale Draft estimates.

UX audit F-47 / 2026-04-29. Per-tenant policy `estimate_draft_archive_days`
in TenantSettings (default 60, 0 disables). Soft-deletes drafts whose
updated_at is older than that threshold. Logs per-tenant counts.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import text

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.core.tenant import company_id
from gdx_dispatch.core.tenant_settings import TenantSettings

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


def _archive_days(db, tenant_id: str) -> int:
    """The archive threshold from THIS tenant's settings row (default 60).

    Keyed by tenant (GDXA-226): prod's table holds a stale second row, and the
    old unkeyed `LIMIT 1` honoured whichever row came back first. A primary-key
    `get` through the ORM, so the `Uuid` bind matches on SQLite (32 dashless
    hex) as well as Postgres. Not `core/settings_row.read_settings_row`: that
    seeds a missing row and commits, which a nightly reader has no business
    doing. A GDX_TENANT_ID that is not a uuid can key no row, so it gets the
    default rather than another row's value.
    """
    try:
        tid = UUID(str(tenant_id))
    except ValueError:
        log.warning("estimate_archive_tenant_id_not_uuid", extra={"tenant_id": tenant_id})
        return 60
    row = db.get(TenantSettings, tid)
    return int((row.estimate_draft_archive_days if row else None) or 60)


@celery_app.task(name="estimates.archive_stale_drafts_for_all_tenants", queue="priority:low")
def archive_stale_drafts_for_all_tenants() -> dict:
    """Soft-delete stale drafts, honoring the per-tenant archive threshold."""
    # The app's own resolver, not this module's "gdx" fallback: with
    # GDX_TENANT_ID unset the app keys its settings row under company_id()'s
    # default, and a keyed read has to ask for that same id.
    tenant_id = company_id()
    total = 0
    try:
        db = SessionLocal()
        try:
            days = _archive_days(db, tenant_id)
        finally:
            db.close()
        total += _archive_for_tenant(tenant_id, days)
    except Exception:
        log.exception("archive_stale_drafts_for_all_tenants_failed")
    return {"tenants_checked": 1, "estimates_archived": total}
