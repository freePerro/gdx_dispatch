"""Morning planner digest — the first staff-facing scheduled reminder.

Emails a short summary of open planner tasks (with overdue + captured-call
counts) once a day so a note taken on a busy day doesn't quietly scroll away.

Channel history: web push is
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
        from gdx_dispatch.models.tenant_models import AppSettings, Lead, PlannerTask

        now = datetime.now(timezone.utc)
        open_tasks = (
            db.execute(select(PlannerTask).where(PlannerTask.status != "done"))
            .scalars()
            .all()
        )

        settings = (
            db.execute(select(AppSettings).limit(1)).scalar_one_or_none()
            if tid
            else None
        )
        tz_name = getattr(settings, "timezone", None) or "America/Chicago"
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("UTC")
        today = datetime.now(tz).date()

        leads_to_call = (
            db.execute(
                select(Lead).where(
                    Lead.deleted_at.is_(None),
                    Lead.stage.not_in(["won", "lost"]),
                    Lead.follow_up_date.isnot(None),
                    Lead.follow_up_date <= today,
                ).order_by(Lead.follow_up_date.asc(), Lead.created_at.desc())
            )
            .scalars()
            .all()
        )
        overdue_leads = [l for l in leads_to_call if l.follow_up_date and l.follow_up_date < today]

        if not open_tasks and not leads_to_call:
            log.info("planner_digest_nothing_open tenant=%s", tid)
            return {"status": "skipped", "reason": "nothing_open"}

        overdue = [t for t in open_tasks if t.due_date and _aware(t.due_date) < now]
        captures = [t for t in open_tasks if t.source == "quick_capture"]
        cold_leads = _cold_lead_count(db)

        subject = _subject(len(open_tasks), len(overdue), len(leads_to_call))
        html = _html_body(open_tasks, overdue, captures, cold_leads, leads_to_call, today)

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
            "planner_digest_sent tenant=%s provider=%s open=%d overdue=%d leads_to_call=%d",
            tid, provider, len(open_tasks), len(overdue), len(leads_to_call),
        )
        return {
            "status": "sent",
            "provider": provider,
            "open": len(open_tasks),
            "overdue": len(overdue),
            "captures": len(captures),
            "cold_leads": cold_leads,
            "leads_to_call": len(leads_to_call),
            "leads_overdue": len(overdue_leads),
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


def _subject(open_count: int, overdue_count: int, leads_count: int = 0) -> str:
    parts = []
    if open_count or overdue_count or leads_count == 0:
        if overdue_count:
            parts.append(f"{open_count} open, {overdue_count} overdue")
        else:
            parts.append(f"{open_count} open")
    if leads_count:
        parts.append(f"{leads_count} lead{'s' if leads_count != 1 else ''} to call back")
    return f"GDX planner: {' · '.join(parts)}"


def _html_body(
    open_tasks,
    overdue,
    captures,
    cold_leads: int,
    leads_to_call: list | None = None,
    today: Any = None,
) -> str:
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

    leads_section = ""
    leads_list = leads_to_call or []
    if leads_list:
        def _lead_row(l) -> str:
            contact = l.phone or l.email or ""
            contact_str = f" <span style='color:#64748b'>({_esc(contact)})</span>" if contact else ""
            is_overdue = today and l.follow_up_date and l.follow_up_date < today
            badge = " <span style='color:#dc2626;font-weight:bold'>[OVERDUE]</span>" if is_overdue else ""
            due_str = f" <span style='color:#64748b'>· due {l.follow_up_date.isoformat()}</span>" if l.follow_up_date else ""
            return f"<li style='margin:4px 0'><b>{_esc(l.name or '(unnamed lead)')}</b>{contact_str}{badge}{due_str}</li>"

        lead_rows = "".join(_lead_row(l) for l in leads_list[:15])
        leads_more = len(leads_list) - 15
        leads_more_line = f"<p style='color:#64748b'>…and {leads_more} more leads.</p>" if leads_more > 0 else ""
        leads_section = (
            f"<div style='margin-top:16px;padding-top:12px;border-top:1px solid #e2e8f0'>"
            f"<p style='font-size:14px;color:#0f172a'><b>Leads to call back ({len(leads_list)}):</b></p>"
            f"<ul style='padding-left:18px'>{lead_rows}</ul>"
            f"{leads_more_line}"
            f"</div>"
        )

    parts = [
        "<div style='font-family:system-ui,Segoe UI,Arial,sans-serif;font-size:15px;color:#0f172a'>",
        "<p>Good morning — here's your planner:</p>",
        "<p style='font-size:14px;color:#475569'>",
        f"<b>{len(open_tasks)}</b> open · <b>{len(overdue)}</b> overdue · "
        f"<b>{len(captures)}</b> from calls",
    ]
    if len(leads_list):
        parts.append(f" · <b>{len(leads_list)}</b> leads to call")
    if cold_leads:
        parts.append(f" · <b>{cold_leads}</b> unmatched callers")
    parts.append("</p>")
    if open_tasks:
        parts.append(f"<ul style='padding-left:18px'>{lines}</ul>")
        parts.append(more_line)
    if leads_section:
        parts.append(leads_section)
    # Absolute URL or no link at all — a relative href is dead in every mail
    # client (this one shipped dead for months).
    import os as _os
    _base = (_os.environ.get("GDX_PUBLIC_BASE_URL") or "").rstrip("/")
    if _base:
        parts.append(
            f"<p style='margin-top:16px'><a href='{_base}/mobile/planner'>Open the planner →</a></p>"
        )
    parts.append("</div>")
    return "".join(parts)


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
