"""Sending a pay period's timesheet to whoever does the payroll.

One entry point, `send_period_timesheet`, used by the office's Send button
and (next) by the scheduled send. Both go through the same gate for the
same reason: a period with an open shift or an impossible day is not a
timesheet, it is a draft, and mailing it puts a number in front of somebody
who will act on it.

The gate REFUSES rather than warns, and it has no override. That is a
deliberate narrowing:

  * The three flags are exactly the ones `/exceptions` reports, and this
    repo already decided those thresholds so that a genuine long day does
    NOT trip them — a real 15-hour shift passes. So there is no legitimate
    "I know, send it anyway" case that a correction would not also fix.
  * The fix IS the dismissal. Correcting the shift on Timesheets clears the
    flag, which is the contract the Dispatch exceptions card is built on.
  * An override button is the thing that gets clicked at 4:55pm on payday.

What leaves the app is hours. No rates, no gross, no overtime — see
core/timesheet_export.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from gdx_dispatch.core.pay_periods import (
    PayPeriod,
    parse_recipient_emails,
    pay_date,
    resolve_zone,
    settings_config,
)
from gdx_dispatch.core.timesheet_export import (
    build_csv,
    build_pdf,
    csv_filename,
    pdf_filename,
)
from gdx_dispatch.core.timesheet_hours import FLAG_LABELS, PeriodTimesheet

log = logging.getLogger(__name__)

#: Stable codes. The UI turns these into a sentence; the scheduled send
#: turns them into a notification. Never a bare boolean — "it didn't send"
#: with no reason is the shape that gets ignored.
BLOCKED_FLAGGED = "flagged_shifts"
BLOCKED_NO_RECIPIENT = "no_recipient"
BLOCKED_EMPTY = "no_hours"
#: A background send with nobody's mailbox to send FROM. Outlook Graph
#: authenticates as a specific person, so an unattended send needs a
#: configured sender or it cannot deliver at all — see below.
BLOCKED_NO_SENDER = "no_sender"


@dataclass
class SendOutcome:
    """What happened, in enough detail to tell somebody."""

    sent: bool
    blocked: str | None = None
    detail: str = ""
    recipients: list[str] = field(default_factory=list)
    delivered_to: list[str] = field(default_factory=list)
    failed_to: list[str] = field(default_factory=list)
    flagged: list[dict[str, str]] = field(default_factory=list)
    period: dict[str, str] = field(default_factory=dict)
    hours: float = 0.0
    people: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "blocked": self.blocked,
            "detail": self.detail,
            "recipients": self.recipients,
            "delivered_to": self.delivered_to,
            "failed_to": self.failed_to,
            "flagged": self.flagged,
            "period": self.period,
            "hours": self.hours,
            "people": self.people,
        }


def _looks_like_user_id(value: str) -> bool:
    """Is this a real user id Graph can authenticate as?

    Deliberately a UUID check and not a truthiness check: the label
    "system:payroll-timesheet" is perfectly truthy and is exactly the value
    that made the send silently undeliverable.
    """
    try:
        UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def describe_flags(sheet: PeriodTimesheet) -> list[dict[str, str]]:
    """Who, which day, and why — the list a person can act on.

    A count alone ("3 shifts need a look") sends the operator hunting.
    """
    return [
        {
            "name": card.name,
            "tech_id": card.tech_id,
            "date": shift.day.isoformat(),
            "reason": FLAG_LABELS.get(shift.flag, shift.flag or ""),
            "entry_id": shift.entry_id,
        }
        for card, shift in sheet.flagged
    ]


def _body_html(
    sheet: PeriodTimesheet, branding: dict[str, str], paid_on: str
) -> str:
    from gdx_dispatch.core.email_layout import esc, render_email

    rows = "".join(
        f'<tr><td style="padding:6px 0;">{esc(card.name)}</td>'
        f'<td style="padding:6px 0;text-align:right;font-weight:600;">'
        f"{card.worked_hours:.2f}</td></tr>"
        for card in sheet.timecards
    )
    company = esc(branding.get("company_name") or "")
    body = f"""
      <p style="margin:0 0 14px;">Hours for {esc(sheet.period.label())}
      {f"— to be paid {esc(paid_on)}" if paid_on else ""}.</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;font-size:15px;">
        {rows}
        <tr><td colspan="2" style="border-top:1px solid #cbd5e1;height:1px;"></td></tr>
        <tr>
          <td style="padding:8px 0;font-weight:700;">Total</td>
          <td style="padding:8px 0;text-align:right;font-weight:700;">
            {sheet.worked_hours:.2f}</td>
        </tr>
      </table>
      <p style="margin:16px 0 0;font-size:14px;color:#475569;">
        Attached: a printable timesheet (PDF) and the same hours as a
        spreadsheet (CSV). Worked hours are clocked time less recorded
        breaks. Hours only — no rates or pay are calculated here.
      </p>
    """
    return render_email(
        branding=branding,
        body_html=body,
        # Staff mail: no "leave us a Google review" footer on a payroll sheet.
        review_ask=False,
        title=f"Timesheet {sheet.period.label()}",
        preheader=(
            f"{sheet.worked_hours:.2f} hours for {sheet.people} "
            f"{'person' if sheet.people == 1 else 'people'} — {company}"
        ),
    )


def _attachments(sheet: PeriodTimesheet, branding: dict[str, str], paid_on: str,
                 prepared_at: str) -> list[dict[str, Any]]:
    pdf = build_pdf(
        sheet, branding=branding, pay_date=paid_on, prepared_at=prepared_at
    )
    csv_text = build_csv(sheet)
    return [
        {
            "name": pdf_filename(sheet.period),
            "content_type": "application/pdf",
            "content_base64": base64.b64encode(pdf).decode("ascii"),
        },
        {
            "name": csv_filename(sheet.period),
            "content_type": "text/csv",
            "content_base64": base64.b64encode(csv_text.encode("utf-8")).decode("ascii"),
        },
    ]


def send_period_timesheet(
    db: Session,
    *,
    tenant_id: str,
    settings: Any,
    sheet: PeriodTimesheet,
    actor_user_id: str,
    initiator_kind: str = "user",
    sender_user_id: str | None = None,
) -> SendOutcome:
    """Mail the period's timesheet, or refuse and say why.

    `settings` is the AppSettings row; `sheet` is already built, so the
    caller decides which period and this decides whether it may go.

    `actor_user_id` is WHO caused the send — it goes in the audit trail and
    may be a non-UUID label like "system:payroll-timesheet".
    `sender_user_id` is WHOSE MAILBOX it leaves from, and must be a real
    user UUID: `send_transactional_email` authenticates to Outlook Graph as
    a specific person and skips that provider entirely when it cannot parse
    one. When omitted, the actor is used — correct for the office's Send
    button, where the two are the same person.

    They are separate parameters because conflating them is what made the
    first scheduled send undeliverable: it passed its own label as the
    sender, Graph was skipped, SMTP is not configured here, and every
    attempt failed with "no_email_provider_connected" while the schedule
    looked healthy. Caught on prod, 2026-08-26.
    """
    period: PayPeriod = sheet.period
    cfg = settings_config(settings)
    paid_on = pay_date(period, cfg["lag_days"]).isoformat()
    outcome = SendOutcome(
        sent=False,
        period=period.as_dict(),
        hours=sheet.worked_hours,
        people=sheet.people,
        flagged=describe_flags(sheet),
    )

    recipients = parse_recipient_emails(
        getattr(settings, "payroll_recipient_emails", "")
    )
    outcome.recipients = recipients

    # Order matters: report the fixable problem, not the first one found.
    # A period with flagged shifts AND no recipient needs the flags named,
    # because that is the one the operator is looking at.
    if outcome.flagged:
        outcome.blocked = BLOCKED_FLAGGED
        outcome.detail = (
            f"{len(outcome.flagged)} shift"
            f"{'' if len(outcome.flagged) == 1 else 's'} still need"
            f"{'s' if len(outcome.flagged) == 1 else ''} a look. "
            "Correct them on Timesheets and the hold clears itself."
        )
        return outcome

    if not recipients:
        outcome.blocked = BLOCKED_NO_RECIPIENT
        outcome.detail = (
            "Nobody is set to receive the timesheet. "
            "Add an address in Settings → Pay periods."
        )
        return outcome

    # Graph needs a person to authenticate as. An UNATTENDED send with no
    # configured sender cannot deliver, so say so once here rather than
    # retrying hourly against a provider that will never be chosen.
    #
    # Only the unattended path is gated. A person pressing Send already gets
    # an honest 502 carrying the provider's own reason, and gating them too
    # would refuse the send on any deployment whose user ids are not UUIDs —
    # breaking a working path to guard against a different one.
    sender = str(sender_user_id or actor_user_id or "")
    if initiator_kind != "user" and not _looks_like_user_id(sender):
        outcome.blocked = BLOCKED_NO_SENDER
        outcome.detail = (
            "No mailbox is set to send from. Choose the sending user in "
            "Settings \u2192 Automated email, then the timesheet can go out."
        )
        return outcome

    if sheet.people == 0:
        # Not an error — a genuinely empty fortnight exists (a shutdown
        # week). But mailing a blank sheet reads as "everyone worked zero
        # hours", so it takes a deliberate act rather than a schedule.
        outcome.blocked = BLOCKED_EMPTY
        outcome.detail = "Nobody clocked any time in this period."
        return outcome

    from gdx_dispatch.core.email_layout import email_branding

    branding = email_branding(db)
    tz_name = getattr(settings, "timezone", None) or "America/New_York"
    prepared = (
        datetime.now(UTC).astimezone(resolve_zone(tz_name)).strftime("%b %-d, %Y %-I:%M %p")
    )
    attachments = _attachments(sheet, branding, paid_on, prepared)
    html = _body_html(sheet, branding, paid_on)
    subject = (
        f"Timesheet {period.start.isoformat()} to {period.end.isoformat()}"
        f" — {sheet.worked_hours:.2f} hours"
    )

    from gdx_dispatch.core.transactional_email import send_transactional_email

    for address in recipients:
        try:
            sent, _provider, skip = send_transactional_email(
                tenant_db=db,
                tenant_id=tenant_id,
                user_id=sender,
                to_email=address,
                to_name=address,
                subject=subject,
                html_body=html,
                attachments=attachments,
                initiator_kind=initiator_kind,
                initiator_ref=actor_user_id,
                kind="payroll_timesheet",
                entity_type="timesheet",
                entity_id=f"{period.start.isoformat()}..{period.end.isoformat()}",
                # <= 20 chars: outbound_emails.recipient_source is
                # varchar(20) and a longer value aborts the INSERT, which
                # poisons the session and loses the audit row with it.
                recipient_source="payroll_settings",
            )
        except Exception:  # noqa: BLE001 — one bad address must not lose the rest
            log.exception("timesheet_send_failed", extra={"tenant_id": tenant_id})
            sent, skip = False, "exception"
        if sent:
            outcome.delivered_to.append(address)
        else:
            outcome.failed_to.append(address)
            if skip and not outcome.detail:
                outcome.detail = f"Mail was not accepted ({skip})."

    # Partial success is success-with-a-list, never a bare True: an operator
    # who is told "sent" while one of two recipients silently failed has no
    # reason to look.
    outcome.sent = bool(outcome.delivered_to)
    if not outcome.sent and not outcome.detail:
        outcome.detail = "Mail was not accepted by the mail server."
    return outcome
