"""Email sender — uses tenant's configured email settings to send."""
from __future__ import annotations

import base64
import logging
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.email_layout import (
    DEFAULT_ACCENT,
    cta_button,
    esc,
    line_items_table,
    money,
    nl2br,
    render_email,
    to_plain_text,
    totals_table,
)

log = logging.getLogger(__name__)


def get_email_config(db: Session, tenant_id: str) -> dict[str, Any] | None:
    """Get the tenant's email config. Returns None if not configured."""
    try:
        row = db.execute(
            text("SELECT provider, smtp_host, smtp_port, username, password_enc, from_email, from_name "
                 "FROM email_settings WHERE company_id = :tid"),
            {"tid": tenant_id},
        ).mappings().first()
        if not row or row["provider"] == "disabled":
            return None
        return dict(row)
    except Exception:  # returns None if configuration cannot be retrieved
        logging.getLogger(__name__).exception("get_email_config caught exception")
        return None


def send_email(
    db: Session,
    tenant_id: str,
    to_email: str,
    subject: str,
    html_body: str,
    to_name: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> bool:
    """Send an email using the tenant's configured SMTP settings.
    Returns True on success, False on failure.

    attachments: [{name, content_type, content_base64}] — the same wire shape
    the Outlook send path uses, so callers build one list for both providers.
    """
    config = get_email_config(db, tenant_id)
    if not config:
        log.warning("Email not configured for tenant %s", tenant_id)
        return False

    try:
        pw = base64.b64decode(config["password_enc"]).decode() if config["password_enc"] else ""

        # text+html always travel as an "alternative" pair (accessibility +
        # spam scoring); attachments wrap that pair in a "mixed" envelope.
        alternative = MIMEMultipart("alternative")
        alternative.attach(MIMEText(to_plain_text(html_body), "plain"))
        alternative.attach(MIMEText(html_body, "html"))

        if attachments:
            msg = MIMEMultipart("mixed")
            msg.attach(alternative)
        else:
            msg = alternative
        msg["Subject"] = subject
        msg["From"] = f"{config['from_name']} <{config['from_email']}>"
        msg["To"] = f"{to_name} <{to_email}>" if to_name else to_email

        for att in attachments or []:
            ctype = att.get("content_type") or "application/octet-stream"
            maintype, _, subtype = ctype.partition("/")
            if not subtype:
                maintype, subtype = "application", "octet-stream"
            part = MIMEBase(maintype, subtype)
            part.set_payload(base64.b64decode(att["content_base64"]))
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition", "attachment", filename=att.get("name") or "attachment"
            )
            msg.attach(part)

        with smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=15) as server:
            server.starttls()
            server.login(config["username"], pw)
            server.send_message(msg)

        log.info("Email sent to %s: %s", to_email, subject)
        return True
    except Exception:  # Failure is handled by returning False as per contract.
        log.exception("email_send_failed to=%s subject=%s", to_email, subject)
        return False


def _fallback_branding(company_name: str) -> dict[str, str]:
    """Legacy-caller shim: a builder invoked without a branding dict still
    renders a correct (if unbranded) shell."""
    return {
        "company_name": company_name or "Your Service Company",
        "logo": "",
        "accent": DEFAULT_ACCENT,
        "phone": "",
        "address": "",
        "email": "",
    }


def _tier_summary_html(tiers: list[dict], accent: str) -> str:
    """Proposal-mode body: one row per tier (name + price), no flat line dump.
    The public approval page is where the customer compares details — the
    email's job is to name the options and route them to the CTA."""
    cell = "font-family:Arial,Helvetica,sans-serif;padding:12px 8px;border-bottom:1px solid #e5e7eb;"
    rows = ""
    for tier in tiers or []:
        name = nl2br(tier.get("name") or "Option")
        desc = nl2br(tier.get("description") or "")
        desc_html = (
            f'<br><span style="font-size:13px;color:#556270;">{desc}</span>' if desc else ""
        )
        rows += f"""<tr>
      <td style="{cell}font-size:15px;color:#1f2937;">{name}{desc_html}</td>
      <td style="{cell}font-size:16px;font-weight:700;color:{esc(accent)};text-align:right;white-space:nowrap;" width="120">{money(tier.get('price', 0))}</td>
    </tr>"""
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:collapse;margin:16px 0;">{rows}</table>"""


def build_estimate_email_html(
    company_name: str,
    estimate_number: str,
    customer_name: str,
    line_items: list[dict],
    total: float,
    notes: str = "",
    portal_url: str = "",
    description: str = "",
    *,
    branding: dict[str, str] | None = None,
    intro_html: str | None = None,
    tiers: list[dict] | None = None,
    valid_until_text: str = "",
    hide_prices: bool = False,
) -> str:
    """Branded estimate email.

    - intro_html: pre-rendered (escaped) copy from the tenant email template;
      None → the default greeting. Replaces the old hardcoded "Dear {name}".
    - tiers: proposal-mode summary [{name, price, description}] — when given,
      the flat line table and single total are suppressed (a tier proposal
      has no meaningful single total; the CTA is the star).
    - valid_until_text: human date for the validity line. Empty → no line.
      (The old hardcoded "valid for 30 days" lied — real expiry is tenant-
      configured; the caller passes the actual date.)
    """
    b = branding or _fallback_branding(company_name)
    accent = b.get("accent") or DEFAULT_ACCENT

    if intro_html is None:
        intro_html = (
            f"<p style=\"margin:0 0 12px;\">Dear {esc(customer_name or 'Valued Customer')},</p>"
            f"<p style=\"margin:0 0 12px;\">Thank you for your interest. Here is your estimate:</p>"
        )

    parts: list[str] = []
    parts.append(
        f'<h2 style="margin:0 0 16px;font-size:20px;color:{esc(accent)};">'
        f"Estimate #{esc(estimate_number)}</h2>"
    )
    parts.append(intro_html)
    if description:
        parts.append(
            f'<p style="margin:0 0 12px;"><strong>Description of Work:</strong><br>{nl2br(description)}</p>'
        )
    if tiers:
        parts.append(_tier_summary_html(tiers, accent))
    else:
        parts.append(line_items_table(line_items, hide_prices=hide_prices))
        parts.append(totals_table([("Total", money(total), True)], accent))
    if notes:
        parts.append(f'<p style="margin:12px 0;"><strong>Notes:</strong> {nl2br(notes)}</p>')
    if portal_url:
        label = "View Options & Choose Online" if tiers else "View & Accept Estimate"
        parts.append(cta_button(portal_url, label, accent))
    if valid_until_text:
        parts.append(
            f'<p style="margin:16px 0 0;font-size:13px;color:#556270;">'
            f"This estimate is valid until {esc(valid_until_text)}. "
            f"Please contact us if you have any questions.</p>"
        )
    else:
        parts.append(
            '<p style="margin:16px 0 0;font-size:13px;color:#556270;">'
            "Please contact us if you have any questions.</p>"
        )

    return render_email(
        branding=b,
        body_html="\n".join(parts),
        title=f"Estimate #{estimate_number}",
        preheader=f"Estimate #{estimate_number} from {b.get('company_name', '')}",
    )


def build_invoice_email_html(
    company_name: str,
    invoice_number: str,
    customer_name: str,
    line_items: list[dict],
    subtotal: float,
    tax_amount: float,
    total: float,
    balance_due: float,
    due_date: str = "",
    notes: str = "",
    portal_url: str = "",
    tax_rate: float | None = None,
    paid_to_date: float = 0.0,
    credits_applied: float = 0.0,
    *,
    branding: dict[str, str] | None = None,
    intro_html: str | None = None,
    is_receipt: bool = False,
) -> str:
    """Branded invoice email; is_receipt flips the copy to a paid thank-you.

    Settlement rows (Paid to Date / Credits Applied) stay — without them the
    totals don't foot on partially-paid invoices (Tier-9.4).
    """
    b = branding or _fallback_branding(company_name)
    accent = b.get("accent") or DEFAULT_ACCENT

    if intro_html is None:
        greeting = esc(customer_name or "Valued Customer")
        if is_receipt:
            intro_html = (
                f'<p style="margin:0 0 12px;">Dear {greeting},</p>'
                f'<p style="margin:0 0 12px;">Thank you for your payment — this invoice is paid in full. '
                f"A copy is included below for your records.</p>"
            )
        else:
            intro_html = (
                f'<p style="margin:0 0 12px;">Dear {greeting},</p>'
                f'<p style="margin:0 0 12px;">Thank you for your business. Please find your invoice details below:</p>'
            )

    tax_label = "Tax"
    if tax_rate is not None and tax_rate > 0:
        tax_label = f"Tax ({tax_rate * 100:.2f}%)"

    totals_rows: list[tuple[str, str, bool]] = [
        ("Subtotal", money(subtotal), False),
        (tax_label, money(tax_amount), False),
        ("Total", money(total), True),
    ]
    if paid_to_date and paid_to_date > 0:
        totals_rows.append(("Paid to Date", f"-{money(paid_to_date)}", False))
    if credits_applied and credits_applied > 0:
        totals_rows.append(("Credits Applied", f"-{money(credits_applied)}", False))
    totals_rows.append(("Balance Due", money(balance_due), True))

    parts: list[str] = []
    heading = "Payment received" if is_receipt else f"Invoice #{esc(invoice_number)}"
    parts.append(
        f'<h2 style="margin:0 0 16px;font-size:20px;color:{esc(accent)};">{heading}</h2>'
    )
    if is_receipt:
        parts.append(
            f'<p style="margin:0 0 8px;font-size:13px;color:#556270;">Invoice #{esc(invoice_number)}</p>'
        )
    parts.append(intro_html)
    if due_date and not is_receipt:
        parts.append(
            f'<p style="margin:0 0 12px;"><strong>Due Date:</strong> {esc(due_date)}</p>'
        )
    parts.append(line_items_table(line_items))
    parts.append(totals_table(totals_rows, accent))
    if notes:
        parts.append(f'<p style="margin:12px 0;"><strong>Notes:</strong> {nl2br(notes)}</p>')
    if portal_url and not is_receipt:
        parts.append(cta_button(portal_url, "View & Pay Invoice", accent))
    parts.append(
        '<p style="margin:16px 0 0;font-size:13px;color:#556270;">'
        "Please contact us if you have any questions about this invoice.</p>"
    )

    return render_email(
        branding=b,
        body_html="\n".join(parts),
        title=f"Invoice #{invoice_number}",
        preheader=(
            f"Payment received — invoice #{invoice_number}"
            if is_receipt
            else f"Invoice #{invoice_number} from {b.get('company_name', '')}"
        ),
    )
