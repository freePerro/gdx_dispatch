"""Celery task: automated invoice dunning (PR6-billing-capture).

Doug 2026-07-07 decisions, all enforced here:
- OPT-IN, default OFF: the task keys ONLY off ReminderSettings.auto_send_enabled
  (the legacy `enabled` stays the manual/preview feature switch).
- While OFF and not permanently dismissed, a WEEKLY nudge (Mondays) tells
  admin/owner how much overdue money isn't being chased.
- Idempotency = the stored threshold (PaymentReminder.threshold_days) — it
  survives schedule_days edits; manual NULL-threshold logs never suppress
  the robot.
- Per-invoice `dunning_paused` mutes real payment arrangements.
- At most one email per invoice per tick (highest crossed unsent threshold)
  so a long-overdue customer isn't triple-emailed on enable day.

Fail-loudly contract: per-invoice failures are counted and returned, a
settings/tenant read failure raises, and every send outcome lands on the
PaymentReminder row ([delivered]/[skipped: reason]).
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from sqlalchemy import func, select

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.core.next_action import NextAction
from gdx_dispatch.models.tenant_models import Invoice, PaymentReminder

log = logging.getLogger(__name__)

NUDGE_ACTION_TYPE = "dunning_disabled_nudge"
NUDGE_REFERENCE_ID = "weekly"


def _run_auto_sends(db, tenant_id: str) -> dict:
    from gdx_dispatch.routers.invoice_reminders import (
        _get_or_create_settings,
        compute_due_sends,
        send_reminder_email_for_invoice,
    )

    settings = _get_or_create_settings(db, tenant_id)

    if not settings.auto_send_enabled:
        nudged = _weekly_nudge(db, tenant_id, settings)
        return {"mode": "disabled", "nudged": nudged, "sent": 0, "skipped": 0, "errors": 0}

    due = compute_due_sends(db, settings)
    sent_n = skipped_n = error_n = 0
    for item in due:
        inv = item["invoice"]
        try:
            sent, skip_reason = send_reminder_email_for_invoice(
                db, tenant_id, inv, settings, user_id=None
            )
            # Audit round 2: a SKIP must not consume the threshold — with
            # threshold_days recorded on a failed send, an SMTP outage (or a
            # customer with no email yet) would permanently eat that dunning
            # stage via the escalation floor. Skips record with
            # threshold_days=None (log-only keyspace) and retry on the next
            # tick; one visible skip row per day until the config is fixed
            # is the fail-loudly behavior we want.
            db.add(PaymentReminder(
                invoice_id=inv.id,
                customer_id=inv.customer_id,
                stage=item["stage"],
                channel="email",
                sent_at=datetime.now(UTC),
                sent_by="auto-dunning",
                threshold_days=item["threshold_days"] if sent else None,
                notes=(
                    "[delivered]" if sent
                    else f"[skipped: {skip_reason}] (t={item['threshold_days']})"
                ),
            ))
            db.commit()
            if sent:
                sent_n += 1
            else:
                skipped_n += 1
        except Exception:
            db.rollback()
            error_n += 1
            log.exception(
                "auto_dunning_send_failed invoice=%s threshold=%s",
                inv.id, item["threshold_days"],
            )
    return {
        "mode": "enabled",
        "due": len(due),
        "sent": sent_n,
        "skipped": skipped_n,
        "errors": error_n,
    }


def _weekly_nudge(db, tenant_id: str, settings) -> bool:
    """Mondays only: one persistent 'dunning is off' NextAction with the
    live overdue picture. Permanently suppressed once dismissed — not
    everyone wants a robot emailing their customers (Doug 2026-07-07)."""
    if settings.auto_send_nudge_dismissed:
        return False
    if datetime.now(UTC).weekday() != 0:
        return False

    # Audit round 2 (zombie fix): a nudge the office COMPLETED this week
    # stays gone until next week's fresh look — completed rows were excluded
    # from the dedup query, so every Monday-after-completion inserted a new
    # zombie. The permanent opt-out is the dismissed flag (a real control on
    # the Invoice Reminders settings screen).
    from datetime import timedelta as _td
    recent_completed = db.execute(
        select(NextAction).where(
            NextAction.tenant_id == tenant_id,
            NextAction.action_type == NUDGE_ACTION_TYPE,
            NextAction.status == "completed",
            NextAction.created_at > datetime.now(UTC) - _td(days=6),
        )
    ).scalars().first()
    if recent_completed is not None:
        return False

    row = db.execute(
        select(func.count(Invoice.id), func.coalesce(func.sum(Invoice.balance_due), 0)).where(
            Invoice.deleted_at.is_(None),
            Invoice.status == "sent",
            Invoice.balance_due > 0,
            Invoice.due_date.is_not(None),
            Invoice.due_date < datetime.now(UTC).date(),
        )
    ).first()
    overdue_count = int(row[0] or 0)
    overdue_total = float(row[1] or 0)
    if overdue_count == 0:
        return False

    description = (
        f"Automated payment reminders are OFF. {overdue_count} overdue "
        f"invoice(s) totaling ${overdue_total:,.2f} are not being chased. "
        "Turn auto-send on in Invoice Reminder settings (you'll see exactly "
        "who gets emailed first), or dismiss this permanently if you don't "
        "want automated dunning."
    )
    existing = db.execute(
        select(NextAction).where(
            NextAction.tenant_id == tenant_id,
            NextAction.action_type == NUDGE_ACTION_TYPE,
            NextAction.reference_id == NUDGE_REFERENCE_ID,
            NextAction.status.notin_(("completed",)),
            NextAction.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.description = description
        existing.estimated_value = overdue_total
        db.commit()
        return True
    db.add(NextAction(
        tenant_id=tenant_id,
        user_id=None,
        action_type=NUDGE_ACTION_TYPE,
        title="Automated payment reminders are off",
        description=description,
        priority="medium",
        action_url="/invoice-reminders",
        estimated_value=overdue_total,
        reference_id=NUDGE_REFERENCE_ID,
    ))
    db.commit()
    return True


@celery_app.task(name="invoice_reminders.auto_dunning_tick")
def auto_dunning_tick() -> dict:
    tenant_id = os.getenv("GDX_TENANT_ID") or os.getenv("GDX_DEFAULT_TENANT_ID") or "gdx"
    db = SessionLocal()
    try:
        result = {"tenant_id": tenant_id, **_run_auto_sends(db, tenant_id)}
        log.info("auto_dunning_tick %s", result)
        return result
    except Exception:
        log.exception("auto_dunning_tick_failed tenant=%s", tenant_id)
        raise
    finally:
        db.close()
