"""Celery task: the daily billing follow-up loop (PR5-billing-capture).

The batch's enforcement layer. PRs 1-4 made every leak class VISIBLE
(Ready-to-Bill, unsent drafts, unbilled approved change orders, parts used
but never billed) — this loop makes sure the office actually sees them:
once a day it counts each class and upserts ONE persistent NextAction (the
existing queue surface) summarizing what's stalled. It clears itself when
everything is billed.

Fail-loudly contract: the task result dict always carries per-class counts
plus dollar totals — a run that found nothing returns explicit zeros, and a
tenant read failure raises (Celery marks the run failed) instead of
pretending the books are clean.

Single-tenant dispatch (GDX_TENANT_ID) — same pattern as
tasks/recurring_billing.py.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from gdx_dispatch.core.billing_predicates import job_billed_exists
from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.core.next_action import NextAction
from gdx_dispatch.models.tenant_models import Invoice, Job, JobPartNeeded

log = logging.getLogger(__name__)

# A job/draft has to sit this long before the loop nags about it — same-day
# billing shouldn't trip the alarm.
STALE_DAYS = 3

ACTION_TYPE = "billing_followup"
REFERENCE_ID = "daily"


def _compute_counts(db) -> dict:
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=STALE_DAYS)

    # Audit catch (round 1 of this PR): QB-imported jobs land completed with
    # NULL completed_at — a bare completed_at filter made them invisible to
    # this loop FOREVER while Ready-for-Billing showed them. Same coalesce
    # reports.py uses.
    ready_to_bill = int(db.scalar(
        select(func.count(Job.id)).where(
            Job.deleted_at.is_(None),
            Job.lifecycle_stage == "completed",
            func.coalesce(Job.completed_at, Job.created_at) < cutoff,
            ~job_billed_exists(),
        )
    ) or 0)

    # $0 drafts are the fabricated create-invoice-from-job placeholders —
    # their jobs already count under ready_to_bill (the canonical predicate
    # treats a $0 draft as unbilled); counting them here double-nags and
    # renders as "$0.00" noise (audit catch).
    stale_drafts = db.execute(
        select(func.count(Invoice.id), func.coalesce(func.sum(Invoice.total), 0)).where(
            Invoice.deleted_at.is_(None),
            Invoice.status == "draft",
            Invoice.total > 0,
            Invoice.created_at < cutoff,
        )
    ).first()
    stale_draft_count = int(stale_drafts[0] or 0)
    stale_draft_total = float(stale_drafts[1] or 0)

    # Approved, never-billed change orders (PR3's checklist feed).
    from gdx_dispatch.routers.change_orders import ChangeOrder
    unbilled_cos = db.execute(
        select(func.count(ChangeOrder.id), func.coalesce(func.sum(ChangeOrder.amount), 0)).where(
            ChangeOrder.deleted_at.is_(None),
            ChangeOrder.status == "approved",
            ChangeOrder.billed_invoice_id.is_(None),
        )
    ).first()
    unbilled_co_count = int(unbilled_cos[0] or 0)
    unbilled_co_total = float(unbilled_cos[1] or 0)

    # Parts used/received on COMPLETED jobs, never billed (PR4's leak class).
    # Two-step for the String-vs-Uuid job_id join, same as the report
    # endpoint; count-only here.
    part_rows = db.execute(
        select(JobPartNeeded.job_id, func.count(JobPartNeeded.id))
        .where(
            JobPartNeeded.billed_invoice_id.is_(None),
            JobPartNeeded.status.in_(("used", "received")),
        )
        .group_by(JobPartNeeded.job_id)
    ).all()
    unbilled_parts = 0
    if part_rows:
        import uuid as _uuid
        job_ids = []
        for jid, _cnt in part_rows:
            try:
                job_ids.append(_uuid.UUID(str(jid)))
            except (ValueError, AttributeError):
                continue
        completed_ids = {
            str(r) for r in db.execute(
                select(Job.id).where(
                    Job.id.in_(job_ids),
                    Job.lifecycle_stage == "completed",
                    Job.deleted_at.is_(None),
                )
            ).scalars().all()
        }
        unbilled_parts = sum(
            int(cnt) for jid, cnt in part_rows if str(jid) in completed_ids
        )

    return {
        "ready_to_bill": ready_to_bill,
        "stale_drafts": stale_draft_count,
        "stale_draft_total": round(stale_draft_total, 2),
        "unbilled_change_orders": unbilled_co_count,
        "unbilled_change_order_total": round(unbilled_co_total, 2),
        "unbilled_parts": unbilled_parts,
    }


def _upsert_action(db, tenant_id: str, counts: dict) -> str:
    """One persistent NextAction, updated in place. When everything is
    clear, an existing open action is completed (the nag clears itself)."""
    total_items = (
        counts["ready_to_bill"]
        + counts["stale_drafts"]
        + counts["unbilled_change_orders"]
        + counts["unbilled_parts"]
    )
    existing = db.execute(
        select(NextAction).where(
            NextAction.tenant_id == tenant_id,
            NextAction.action_type == ACTION_TYPE,
            NextAction.reference_id == REFERENCE_ID,
            NextAction.status.notin_(("completed",)),
            NextAction.deleted_at.is_(None),
        )
    ).scalar_one_or_none()

    if total_items == 0:
        if existing is not None:
            existing.status = "completed"
            existing.completed_at = datetime.now(UTC)
            db.commit()
            return "cleared"
        return "clean"

    pieces = []
    if counts["ready_to_bill"]:
        pieces.append(f"{counts['ready_to_bill']} completed job(s) unbilled >{STALE_DAYS}d")
    if counts["stale_drafts"]:
        pieces.append(
            f"{counts['stale_drafts']} draft invoice(s) never sent (${counts['stale_draft_total']:,.2f})"
        )
    if counts["unbilled_change_orders"]:
        pieces.append(
            f"{counts['unbilled_change_orders']} approved change order(s) unbilled (${counts['unbilled_change_order_total']:,.2f} pre-tax)"
        )
    if counts["unbilled_parts"]:
        pieces.append(f"{counts['unbilled_parts']} used part(s) never billed")
    description = "Money is sitting in the pipeline: " + "; ".join(pieces) + ". Review on /billing."

    if existing is not None:
        existing.title = "Billing follow-up: work is waiting to be billed"
        existing.description = description
        existing.priority = "high"
        existing.action_url = "/billing"
        existing.estimated_value = counts["stale_draft_total"] + counts["unbilled_change_order_total"]
        # Respect a live snooze (audit catch): the office deliberately parked
        # the nag — refresh its numbers but don't force-wake it every day.
        _su = existing.snoozed_until
        if _su is not None and _su.tzinfo is None:
            # SQLite returns naive datetimes from tz-aware columns.
            _su = _su.replace(tzinfo=UTC)
        is_snoozed = (
            existing.status == "snoozed"
            and _su is not None
            and _su > datetime.now(UTC)
        )
        if not is_snoozed:
            existing.status = "pending"
        db.commit()
        return "updated"

    db.add(NextAction(
        tenant_id=tenant_id,
        user_id=None,
        action_type=ACTION_TYPE,
        title="Billing follow-up: work is waiting to be billed",
        description=description,
        priority="high",
        action_url="/billing",
        estimated_value=counts["stale_draft_total"] + counts["unbilled_change_order_total"],
        reference_id=REFERENCE_ID,
    ))
    db.commit()
    return "created"


@celery_app.task(name="billing_followup.daily_tick")
def billing_followup_tick() -> dict:
    """Daily loop: count every billing leak class, upsert the nag action."""
    tenant_id = os.getenv("GDX_TENANT_ID") or os.getenv("GDX_DEFAULT_TENANT_ID") or "gdx"
    db = SessionLocal()
    try:
        counts = _compute_counts(db)
        outcome = _upsert_action(db, tenant_id, counts)
        result = {"tenant_id": tenant_id, "outcome": outcome, **counts}
        log.info("billing_followup_tick %s", result)
        return result
    except Exception:
        # Fail LOUDLY — a broken follow-up loop that returns success is the
        # dead-recurring-billing failure mode this batch exists to end.
        log.exception("billing_followup_tick_failed tenant=%s", tenant_id)
        raise
    finally:
        db.close()
