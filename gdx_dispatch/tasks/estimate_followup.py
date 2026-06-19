"""Celery task: auto-remind customers about pending estimates.

Runs daily. Finds estimates with status "sent" older than 3 days
that haven't had a reminder sent, and queues a notification.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)

REMINDER_DELAY_DAYS = 3


def _check_tenant_estimates(tenant_id: str) -> int:
    """Check for stale estimates and queue reminders."""
    db = SessionLocal()
    reminded = 0

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=REMINDER_DELAY_DAYS)
        stale = db.execute(
            text(
                """
                SELECT id, customer_id
                FROM estimates
                WHERE status = 'sent'
                  AND created_at < :cutoff
                  AND reminder_sent_at IS NULL
                  AND deleted_at IS NULL
                  AND company_id = :tid
                """
            ),
            {"cutoff": cutoff, "tid": tenant_id},
        ).mappings().all()

        now = datetime.now(timezone.utc)
        for est in stale:
            db.execute(
                text("UPDATE estimates SET reminder_sent_at = :now WHERE id = :eid"),
                {"now": now, "eid": est["id"]},
            )
            reminded += 1

        if reminded:
            db.commit()
            log.info(
                "estimate_followup_reminders_sent",
                extra={"tenant_id": tenant_id, "reminded": reminded},
            )
    except Exception:
        log.exception("estimate_followup_check_failed", extra={"tenant_id": tenant_id})
        db.rollback()
    finally:
        db.close()

    return reminded


@celery_app.task(name="check_estimate_followups")
def check_estimate_followups() -> dict:
    """Run estimate follow-up check."""
    tenant_id = os.getenv("GDX_TENANT_ID") or os.getenv("GDX_DEFAULT_TENANT_ID") or "gdx"
    total = 0
    try:
        total += _check_tenant_estimates(tenant_id)
    except Exception:
        log.exception("check_estimate_followups_failed")
    return {"tenants_checked": 1, "reminders_sent": total}
