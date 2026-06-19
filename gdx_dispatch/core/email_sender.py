"""Email sender — uses tenant's configured email settings to send."""
from __future__ import annotations

import base64
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

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
) -> bool:
    """Send an email using the tenant's configured SMTP settings.
    Returns True on success, False on failure.
    """
    config = get_email_config(db, tenant_id)
    if not config:
        log.warning("Email not configured for tenant %s", tenant_id)
        return False

    try:
        pw = base64.b64decode(config["password_enc"]).decode() if config["password_enc"] else ""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{config['from_name']} <{config['from_email']}>"
        msg["To"] = f"{to_name} <{to_email}>" if to_name else to_email

        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=15) as server:
            server.starttls()
            server.login(config["username"], pw)
            server.send_message(msg)

        log.info("Email sent to %s: %s", to_email, subject)
        return True
    except Exception:  # Failure is handled by returning False as per contract.
        log.exception("email_send_failed to=%s subject=%s", to_email, subject)
        return False


def build_estimate_email_html(
    company_name: str,
    estimate_number: str,
    customer_name: str,
    line_items: list[dict],
    total: float,
    notes: str = "",
    portal_url: str = "",
    description: str = "",
) -> str:
    """Build a professional HTML email for an estimate."""
    items_html = ""
    for li in line_items:
        items_html += f"""<tr>
            <td style="padding:8px;border-bottom:1px solid #eee">{li.get('description','')}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">{li.get('quantity',1)}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;text-align:right">${li.get('unit_price',0):.2f}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;text-align:right">${li.get('line_total',0):.2f}</td>
        </tr>"""

    portal_section = ""
    if portal_url:
        portal_section = f"""<p style="margin:20px 0">
            <a href="{portal_url}" style="background:#0057a8;color:white;padding:12px 24px;text-decoration:none;border-radius:6px;font-weight:600">
                View & Accept Estimate
            </a>
        </p>"""

    return f"""
    <div style="max-width:600px;margin:0 auto;font-family:Arial,sans-serif;color:#333">
        <div style="background:#0057a8;color:white;padding:20px;text-align:center">
            <h1 style="margin:0;font-size:24px">{company_name}</h1>
        </div>
        <div style="padding:20px">
            <h2 style="color:#0057a8;margin-top:0">Estimate #{estimate_number}</h2>
            <p>Dear {customer_name},</p>
            <p>Thank you for your interest. Here is your estimate:</p>
            {"<p><strong>Description of Work:</strong><br>" + description.replace(chr(10), "<br>") + "</p>" if description else ""}
            <table style="width:100%;border-collapse:collapse;margin:20px 0">
                <thead>
                    <tr style="background:#f8f9fa">
                        <th style="padding:8px;text-align:left;border-bottom:2px solid #ddd">Description</th>
                        <th style="padding:8px;text-align:center;border-bottom:2px solid #ddd">Qty</th>
                        <th style="padding:8px;text-align:right;border-bottom:2px solid #ddd">Price</th>
                        <th style="padding:8px;text-align:right;border-bottom:2px solid #ddd">Total</th>
                    </tr>
                </thead>
                <tbody>{items_html}</tbody>
            </table>
            <p style="text-align:right;font-size:18px;font-weight:700">
                Total: <span style="color:#0057a8">${total:.2f}</span>
            </p>
            {"<p><strong>Notes:</strong> " + notes + "</p>" if notes else ""}
            {portal_section}
            <p style="color:#666;font-size:12px;margin-top:30px">
                This estimate is valid for 30 days. Please contact us if you have any questions.
            </p>
        </div>
        <div style="background:#f8f9fa;padding:15px;text-align:center;color:#666;font-size:12px">
            {company_name} — Sent via GDX Platform
        </div>
    </div>
    """


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
) -> str:
    # Mirrors build_estimate_email_html but speaks invoice-language:
    # subtotal + tax breakdown, "Balance Due" call-out, "Pay Now" CTA when
    # a portal URL is supplied. mobile_invoicing.py already imports this
    # symbol with a try/except fallback; the canonical send_invoice path
    # in routers/invoices.py uses it post-S110.
    items_html = ""
    for li in line_items:
        items_html += f"""<tr>
            <td style="padding:8px;border-bottom:1px solid #eee">{li.get('description','')}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;text-align:center">{li.get('quantity',1)}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;text-align:right">${li.get('unit_price',0):.2f}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;text-align:right">${li.get('line_total',0):.2f}</td>
        </tr>"""

    portal_section = ""
    if portal_url:
        portal_section = f"""<p style="margin:20px 0">
            <a href="{portal_url}" style="background:#0057a8;color:white;padding:12px 24px;text-decoration:none;border-radius:6px;font-weight:600">
                View & Pay Invoice
            </a>
        </p>"""

    tax_label = "Tax"
    if tax_rate is not None and tax_rate > 0:
        tax_label = f"Tax ({tax_rate * 100:.2f}%)"

    due_section = ""
    if due_date:
        due_section = (
            f"<p style=\"margin:8px 0\"><strong>Due Date:</strong> {due_date}</p>"
        )

    return f"""
    <div style="max-width:600px;margin:0 auto;font-family:Arial,sans-serif;color:#333">
        <div style="background:#0057a8;color:white;padding:20px;text-align:center">
            <h1 style="margin:0;font-size:24px">{company_name}</h1>
        </div>
        <div style="padding:20px">
            <h2 style="color:#0057a8;margin-top:0">Invoice #{invoice_number}</h2>
            <p>Dear {customer_name},</p>
            <p>Thank you for your business. Please find your invoice details below:</p>
            {due_section}
            <table style="width:100%;border-collapse:collapse;margin:20px 0">
                <thead>
                    <tr style="background:#f8f9fa">
                        <th style="padding:8px;text-align:left;border-bottom:2px solid #ddd">Description</th>
                        <th style="padding:8px;text-align:center;border-bottom:2px solid #ddd">Qty</th>
                        <th style="padding:8px;text-align:right;border-bottom:2px solid #ddd">Price</th>
                        <th style="padding:8px;text-align:right;border-bottom:2px solid #ddd">Total</th>
                    </tr>
                </thead>
                <tbody>{items_html}</tbody>
            </table>
            <table style="width:100%;border-collapse:collapse;margin:8px 0">
                <tr>
                    <td style="padding:4px 8px;text-align:right;color:#555">Subtotal</td>
                    <td style="padding:4px 8px;text-align:right;width:110px">${subtotal:.2f}</td>
                </tr>
                <tr>
                    <td style="padding:4px 8px;text-align:right;color:#555">{tax_label}</td>
                    <td style="padding:4px 8px;text-align:right">${tax_amount:.2f}</td>
                </tr>
                <tr>
                    <td style="padding:6px 8px;text-align:right;font-weight:700;border-top:1px solid #ddd">Total</td>
                    <td style="padding:6px 8px;text-align:right;font-weight:700;border-top:1px solid #ddd">${total:.2f}</td>
                </tr>
                <tr>
                    <td style="padding:6px 8px;text-align:right;color:#0057a8;font-weight:700">Balance Due</td>
                    <td style="padding:6px 8px;text-align:right;color:#0057a8;font-weight:700;font-size:18px">${balance_due:.2f}</td>
                </tr>
            </table>
            {"<p><strong>Notes:</strong> " + notes + "</p>" if notes else ""}
            {portal_section}
            <p style="color:#666;font-size:12px;margin-top:30px">
                Please contact us if you have any questions about this invoice.
            </p>
        </div>
        <div style="background:#f8f9fa;padding:15px;text-align:center;color:#666;font-size:12px">
            {company_name} — Sent via GDX Platform
        </div>
    </div>
    """
