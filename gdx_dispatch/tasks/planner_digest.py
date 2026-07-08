"""Morning planner digest — the first staff-facing scheduled reminder.

Emails a short summary of open planner tasks (with overdue + captured-call
counts) once a day so a note taken on a busy day doesn't quietly scroll away.

Channel history (see docs/design/call-capture-followup-plan.md): web push is
not functional in prod (no VAPID keys / no PWA manifest / iOS can't receive it
in a plain tab) and the Phone.com SMS path is deliberately P2P-only (automated
sends risk carrier-blocking the number until 10DLC clears). Email via
``send_transactional_email`` (Outlook Graph → SMTP fallback) is the one channel
live in prod and reaches Doug's phone through the mail app for free.

Config (all env, no business identifiers hardcoded — public repo):
  PLANNER_DIGEST_EMAIL  recipient; unset ⇒ the digest no-ops.
  PLANNER_DIGEST_HOUR   UTC hour for the beat entry (default 13 ≈ morning US
                        Central). Celery has no timezone configured, so beat
                        fires in UTC.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal

log = logging.getLogger(__name__)

_TENANT_ID = os.getenv("GDX_TENANT_ID", "")
_DIGEST_EMAIL = os.getenv("PLANNER_DIGEST_EMAIL", "")


@celery_app.task(queue="priority:low")
def send_planner_digest(tenant_id: str = "", to_email: str = "") -> dict[str, Any]:
    tid = tenant_id or _TENANT_ID
    recipient = to_email or _DIGEST_EMAIL
    if not recipient:
        log.info("planner_digest_skipped_no_recipient")
        return {"status": "skipped", "reason": "no_recipient"}

    db = SessionLocal()
    try:
        from gdx_dispatch.models.tenant_models import PlannerTask

        now = datetime.now(timezone.utc)
        open_tasks = (
            db.execute(select(PlannerTask).where(PlannerTask.status != "done"))
            .scalars()
            .all()
        )
        if not open_tasks:
            log.info("planner_digest_nothing_open tenant=%s", tid)
            return {"status": "skipped", "reason": "nothing_open"}

        overdue = [t for t in open_tasks if t.due_date and _aware(t.due_date) < now]
        captures = [t for t in open_tasks if t.source == "quick_capture"]
        cold_leads = _cold_lead_count(db)

        subject = _subject(len(open_tasks), len(overdue))
        html = _html_body(open_tasks, overdue, captures, cold_leads)

        from gdx_dispatch.core.transactional_email import send_transactional_email

        sent, provider, reason = send_transactional_email(
            tenant_db=db,
            tenant_id=tid,
            user_id=_digest_sender_user_id(db),
            to_email=recipient,
            to_name="",
            subject=subject,
            html_body=html,
        )
        if not sent:
            # Loudly — the point of this feature is a channel that does NOT
            # silently no-op. log.error routes to Sentry.
            log.error(
                "planner_digest_send_failed tenant=%s reason=%s recipient=%s",
                tid, reason, recipient,
            )
            return {"status": "failed", "reason": reason}

        log.info(
            "planner_digest_sent tenant=%s provider=%s open=%d overdue=%d",
            tid, provider, len(open_tasks), len(overdue),
        )
        return {
            "status": "sent",
            "provider": provider,
            "open": len(open_tasks),
            "overdue": len(overdue),
            "captures": len(captures),
            "cold_leads": cold_leads,
        }
    except Exception:
        log.exception("planner_digest_failed tenant=%s", tid)
        db.rollback()
        raise
    finally:
        db.close()


def _aware(dt: datetime) -> datetime:
    """Treat naive timestamps as UTC so comparisons never raise."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _digest_sender_user_id(db) -> str | None:
    """A connected Outlook mailbox to send as; None ⇒ SMTP fallback."""
    try:
        from gdx_dispatch.modules.outlook.models import OutlookAccount

        row = (
            db.execute(
                select(OutlookAccount)
                .where(
                    OutlookAccount.provider == "outlook",
                    OutlookAccount.refresh_token_enc.isnot(None),
                )
                .order_by(OutlookAccount.connected_at.desc().nullslast())
                .limit(1)
            )
            .scalars()
            .first()
        )
        return row.user_id if row else None
    except Exception:
        log.exception("planner_digest_sender_lookup_failed")
        return None


def _cold_lead_count(db) -> int:
    """Unmatched inbound calls — the 'never called back' leak. Best-effort."""
    try:
        from gdx_dispatch.modules.phone_com.models import PhoneComCall

        return (
            db.query(PhoneComCall)
            .filter(PhoneComCall.direction == "in", PhoneComCall.customer_id.is_(None))
            .count()
        )
    except Exception:
        return 0


def _subject(open_count: int, overdue_count: int) -> str:
    if overdue_count:
        return f"GDX planner: {open_count} open, {overdue_count} overdue"
    return f"GDX planner: {open_count} open"


def _html_body(open_tasks, overdue, captures, cold_leads: int) -> str:
    def _row(t) -> str:
        due = ""
        if t.due_date:
            due = f" <span style='color:#64748b'>· due {_aware(t.due_date).date().isoformat()}</span>"
        flag = " 📞" if t.source == "quick_capture" else ""
        title = (t.title or "(untitled)")[:120]
        return f"<li style='margin:4px 0'>{_esc(title)}{flag}{due}</li>"

    # Overdue first, then the rest — oldest due first mirrors the in-app view.
    overdue_ids = {id(t) for t in overdue}
    rest = [t for t in open_tasks if id(t) not in overdue_ids]
    ordered = sorted(overdue, key=lambda t: _aware(t.due_date)) + rest

    lines = "".join(_row(t) for t in ordered[:25])
    more = len(ordered) - 25
    more_line = f"<p style='color:#64748b'>…and {more} more.</p>" if more > 0 else ""

    parts = [
        "<div style='font-family:system-ui,Segoe UI,Arial,sans-serif;font-size:15px;color:#0f172a'>",
        "<p>Good morning — here's your planner:</p>",
        "<p style='font-size:14px;color:#475569'>",
        f"<b>{len(open_tasks)}</b> open · <b>{len(overdue)}</b> overdue · "
        f"<b>{len(captures)}</b> from calls",
    ]
    if cold_leads:
        parts.append(f" · <b>{cold_leads}</b> unmatched callers")
    parts.append("</p>")
    parts.append(f"<ul style='padding-left:18px'>{lines}</ul>")
    parts.append(more_line)
    parts.append("<p style='margin-top:16px'><a href='/mobile/planner'>Open the planner →</a></p>")
    parts.append("</div>")
    return "".join(parts)


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
