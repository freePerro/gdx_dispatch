"""Nightly estimate-expiry sweep — plan §15 win/loss.

On send, an estimate gets valid_until = sent_at + the tenant's
estimate_expiry_days (default 60). This beat task then marks any still-'sent'
estimate whose valid_until has passed as 'expired', so the pipeline reflects
reality instead of showing months-old quotes as live.

Narrowed to status='sent' on purpose: drafts are never sent to a customer and
never get a valid_until, and accepted/declined are terminal. The manual
POST /api/estimates/expire-stale endpoint stays for on-demand runs.

SCOPE — this is forward-looking. valid_until is only stamped on sends that
happen AFTER this feature ships, so estimates already sitting in 'sent' with a
NULL valid_until are NOT touched by this task and will stay 'live' forever. That
is deliberate: bulk-expiring the existing backlog is a business decision (it can
retire a large dollar amount of open quotes at once) and needs an explicit,
reviewed one-off with Doug's sign-off, not a silent backfill here.
"""
from __future__ import annotations

import logging

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)


@celery_app.task(name="estimates.expire_stale_nightly", queue="priority:low")
def expire_stale_nightly() -> dict:
    """Mark sent estimates past their valid_until as expired. Returns
    {expired_count}. Never raises out of the beat — a read failure is logged
    and reported, not crashed."""
    try:
        with SessionLocal() as db:
            from gdx_dispatch.modules.proposals.models import Estimate

            now = utcnow()
            stale = (
                db.query(Estimate)
                .filter(
                    # "rejected" (email bounced, 2026-08-13) ages out like
                    # sent: the portal presents it as an open estimate and
                    # accept has no valid_until check of its own — without
                    # this, a bounced-never-resent estimate stays acceptable
                    # forever at frozen pricing.
                    Estimate.status.in_(("sent", "rejected")),
                    Estimate.valid_until.isnot(None),
                    Estimate.valid_until < now,
                    Estimate.deleted_at.is_(None),
                )
                .all()
            )
            expired_ids = []
            for est in stale:
                est.status = "expired"
                est.updated_at = now
                expired_ids.append(str(est.id))
            if expired_ids:
                db.commit()
                try:
                    log_audit_event_sync(
                        db,
                        tenant_id="",
                        user_id="system:beat",
                        action="expire_stale_estimates",
                        entity_type="estimate",
                        entity_id="",
                        details={"expired_count": len(expired_ids), "source": "nightly"},
                    )
                    db.commit()
                except Exception:
                    log.exception("expire_stale_nightly_audit_failed")
    except Exception:
        log.exception("expire_stale_nightly_failed_to_run")
        return {"expired_count": 0, "error": "run_failed"}

    if expired_ids:
        log.info("expire_stale_nightly expired %d estimate(s)", len(expired_ids))
    return {"expired_count": len(expired_ids)}
