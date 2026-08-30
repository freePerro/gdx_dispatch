"""Shared email shell — every outbound customer email renders through this.

Before this module, each send surface hand-rolled its own HTML (three
different blues, div-based layouts) with the classic email-client failures:
Outlook's Word renderer doesn't inherit font-family into <td> (Times New
Roman line items), ignores max-width on divs (full-width sprawl) and padding
on anchors (the CTA degrades to a bare link), and dark-mode clients invert
unstyled backgrounds under hardcoded dark text. See
docs/design/email-readability-and-delivery-plan.md for the full audit.

Rules this module encodes (don't undo them piecemeal):
- Table-based 600px shell; width set as BOTH attribute and inline style —
  Outlook honors the attribute, everyone else the style.
- font-family/size/line-height declared on every text-bearing <td>, never
  only on a wrapper.
- Explicit background-color on every surface + a color-scheme meta hint, so
  dark-mode clients transform from a known-light baseline instead of
  inverting transparent containers.
- CTA is a padded <td> with the color on the cell, anchor inside — the one
  button construction Outlook renders as a button.
- Every interpolated value goes through esc()/nl2br(); money through
  core.money_format.format_money. Builders in email_sender.py must not
  interpolate raw values.
"""
from __future__ import annotations

import html as _html
import re
from typing import Any

from sqlalchemy.orm import Session

from gdx_dispatch.core.money_format import format_money

# One default accent when the tenant hasn't set a primary color. Matches the
# AppSettings.primary_color column default family (slate/blue) rather than the
# legacy hardcoded #0057a8.
DEFAULT_ACCENT = "#2563eb"

_FONT = "Arial,Helvetica,sans-serif"
# Body text on white — not pure black; survives dark-mode transforms better.
_TEXT = "#1f2937"
_MUTED = "#556270"


def esc(value: Any) -> str:
    """HTML-escape any value; None → empty string."""
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


def nl2br(value: Any) -> str:
    """Escape then convert newlines to <br> — multi-line descriptions and
    notes keep their line structure instead of collapsing to one run-on."""
    return esc(value).replace("\r\n", "\n").replace("\n", "<br>")


def money(value: Any) -> str:
    return format_money(value)


_URL_RE = re.compile(r"(https?://[^\s<]+)")


def linkify(escaped_html: str, color: str = DEFAULT_ACCENT) -> str:
    """Wrap bare http(s) URLs in already-ESCAPED text with anchors — Outlook
    does not auto-link plain text inside an HTML body (the unclickable
    approval-link bug). Input must be pre-escaped (esc/nl2br output); the
    &amp; entities inside a URL are valid in an href."""
    def _sub(m: re.Match[str]) -> str:
        url = m.group(1)
        return f'<a href="{url}" style="color:{esc(color)};">{url}</a>'

    return _URL_RE.sub(_sub, escaped_html or "")


_HEAD_RE = re.compile(r"<head\b.*?</head>", re.IGNORECASE | re.DOTALL)
_PREHEADER_RE = re.compile(r"<div style=\"display:none[^\"]*\">.*?</div>", re.IGNORECASE | re.DOTALL)
_ANCHOR_RE = re.compile(r"<a\b[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<(?:br|/p|/tr|/h[1-6]|/div|/li)[^>]*>", re.IGNORECASE)
_STRIP_RE = re.compile(r"<[^>]+>")


def to_plain_text(html_body: str) -> str:
    """Naive text alternative for the multipart/alternative SMTP part.
    Not a renderer — just enough for preview panes and text-only clients.

    Order matters: <head> (title) and the hidden preheader are dropped so
    the text doesn't open with the subject repeated three times, and anchors
    become "label: url" BEFORE tags are stripped — a text part whose
    call-to-action has no actionable URL is worse than none (and a
    part-content mismatch is a spam heuristic)."""
    text = _HEAD_RE.sub("", html_body or "")
    text = _PREHEADER_RE.sub("", text)

    def _anchor(m: re.Match[str]) -> str:
        url, label = m.group(1), _STRIP_RE.sub("", m.group(2)).strip()
        url_plain = _html.unescape(url)
        if not label or label == url:
            return url_plain
        return f"{label}: {url_plain}"

    text = _ANCHOR_RE.sub(_anchor, text)
    text = _TAG_RE.sub("\n", text)
    text = _STRIP_RE.sub("", text)
    text = _html.unescape(text)
    lines = [ln.strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln:
            out.append(ln)
        elif out and out[-1]:
            out.append("")
    return "\n".join(out).strip()


def email_branding(db: Session) -> dict[str, str]:
    """Tenant identity for the shell, from AppSettings — the same source the
    PDF generator uses, so emails and PDFs can't diverge. Missing settings
    degrade to neutral defaults, never to a broken header."""
    company_name = ""
    logo = ""
    accent = ""
    phone = ""
    address = ""
    email = ""
    review_url = ""
    try:
        from gdx_dispatch.models.tenant_models import AppSettings

        settings = db.query(AppSettings).first()
        if settings:
            company_name = settings.company_name or ""
            logo = settings.logo or ""
            accent = settings.primary_color or ""
            phone = settings.phone or ""
            address = settings.address or ""
            email = settings.email or ""
            review_url = getattr(settings, "google_review_url", "") or ""
    except Exception:
        # Branding must never block a send — but a read failure means the
        # customer gets an unbranded "Your Service Company" email, so it is
        # loud in the logs, not silent.
        import logging

        logging.getLogger(__name__).exception("email_branding_read_failed")
    return {
        "company_name": company_name or "Your Service Company",
        "logo": _absolute_logo_url(logo),
        "accent": accent or DEFAULT_ACCENT,
        "phone": phone,
        "address": address,
        "email": email,
        # Blank = no review line. Scheme is re-checked at render time.
        "google_review_url": review_url.strip(),
    }


def _absolute_logo_url(logo: str) -> str:
    """Email clients can only load absolute URLs. A relative path is prefixed
    with the public base URL when configured; otherwise the logo is dropped
    and the text header stands in (a broken image icon is worse than text)."""
    import os

    logo = (logo or "").strip()
    if not logo:
        return ""
    if logo.startswith(("http://", "https://")):
        return logo
    base = (os.environ.get("GDX_PUBLIC_BASE_URL") or "").rstrip("/")
    if base:
        return f"{base}/{logo.lstrip('/')}"
    return ""


def cta_button(url: str, label: str, accent: str = DEFAULT_ACCENT) -> str:
    """Bulletproof button: color + padding live on the <td>, so Outlook's
    Word renderer shows a real button instead of a bare text link."""
    if not url:
        return ""
    return f"""<table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="margin:24px auto;">
  <tr>
    <td style="background-color:{esc(accent)};border-radius:6px;mso-padding-alt:13px 28px;">
      <a href="{esc(url)}" style="display:inline-block;padding:13px 28px;font-family:{_FONT};font-size:16px;font-weight:600;color:#ffffff;text-decoration:none;">{esc(label)}</a>
    </td>
  </tr>
</table>"""


def line_items_table(line_items: list[dict], *, hide_prices: bool = False) -> str:
    """Escaped, newline-preserving line-item table. Every <td> declares its
    own font — Outlook does not inherit font-family into table cells.

    hide_prices: the "total-only" presentational override (estimate tri-state
    / tenant default). The PDF honored it; the email now does too — an
    estimate configured to hide per-line prices must not email them."""
    cell = f"font-family:{_FONT};font-size:14px;color:{_TEXT};padding:8px;border-bottom:1px solid #e5e7eb;"
    head = f"font-family:{_FONT};font-size:13px;color:{_MUTED};padding:8px;border-bottom:2px solid #d1d5db;"
    rows = ""
    for li in line_items or []:
        price_cells = ""
        if not hide_prices:
            price_cells = (
                f'<td style="{cell}text-align:right;white-space:nowrap;">{money(li.get("unit_price", 0))}</td>'
                f'<td style="{cell}text-align:right;white-space:nowrap;">{money(li.get("line_total", 0))}</td>'
            )
        rows += f"""<tr>
      <td style="{cell}">{nl2br(li.get('description', ''))}</td>
      <td style="{cell}text-align:center;white-space:nowrap;">{esc(li.get('quantity', 1))}</td>
      {price_cells}
    </tr>"""
    price_heads = ""
    if not hide_prices:
        price_heads = (
            f'<th align="right" style="{head}text-align:right;">Price</th>'
            f'<th align="right" style="{head}text-align:right;">Total</th>'
        )
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:collapse;margin:16px 0;">
  <thead>
    <tr style="background-color:#f8fafc;">
      <th align="left" style="{head}text-align:left;">Description</th>
      <th align="center" style="{head}text-align:center;">Qty</th>
      {price_heads}
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>"""


def totals_table(rows: list[tuple[str, str, bool]], accent: str = DEFAULT_ACCENT) -> str:
    """Right-aligned totals block. rows = [(label, formatted_value, emphasize)].
    Values arrive pre-formatted (money()) so credit/negative styling stays a
    caller decision."""
    cell = f"font-family:{_FONT};font-size:14px;padding:4px 8px;text-align:right;"
    out = ""
    for label, value, emphasize in rows:
        if emphasize:
            style_l = f"{cell}color:{esc(accent)};font-weight:700;border-top:1px solid #d1d5db;padding-top:8px;"
            style_v = f"{cell}color:{esc(accent)};font-weight:700;font-size:17px;border-top:1px solid #d1d5db;padding-top:8px;white-space:nowrap;"
        else:
            style_l = f"{cell}color:{_MUTED};"
            style_v = f"{cell}color:{_TEXT};white-space:nowrap;"
        out += f"""<tr>
      <td style="{style_l}">{esc(label)}</td>
      <td style="{style_v}" width="120">{value}</td>
    </tr>"""
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:collapse;margin:8px 0;">{out}</table>"""


def render_email(
    *,
    branding: dict[str, str],
    body_html: str,
    title: str = "",
    preheader: str = "",
    review_ask: bool = True,
) -> str:
    """Wrap body_html in the branded shell. body_html is TRUSTED markup built
    by the callers in this package from escaped parts — never raw user input.

    review_ask: when True (the default — every customer-facing send) and the
    tenant has set ``google_review_url``, the footer opens with a "Leave us a
    Google review" link. Staff mail (payroll timesheets, platform auth email)
    passes False; asking an employee to review the company on their own
    timesheet is the kind of thing a shared shell does by accident.
    """
    company = esc(branding.get("company_name") or "Your Service Company")
    accent = esc(branding.get("accent") or DEFAULT_ACCENT)
    logo = branding.get("logo") or ""
    if logo:
        header_inner = (
            f'<img src="{esc(logo)}" alt="{company}" height="40" '
            f'style="max-height:40px;display:inline-block;vertical-align:middle;border:0;">'
        )
    else:
        header_inner = (
            f'<span style="font-family:{_FONT};font-size:22px;font-weight:700;'
            f'color:#ffffff;">{company}</span>'
        )

    footer_bits = [company]
    for key in ("phone", "address", "email"):
        val = (branding.get(key) or "").strip()
        if val:
            footer_bits.append(esc(val))
    footer = " &nbsp;&middot;&nbsp; ".join(footer_bits)
    review_url = (branding.get("google_review_url") or "").strip() if review_ask else ""
    # Scheme check here as well as at PATCH time: this value lands verbatim
    # in an href in every customer's inbox, and a row edited outside the API
    # (SQL, a future importer) must not turn the footer into a javascript:
    # link. Anything that is not absolute http(s) is treated as unset.
    if review_url.lower().startswith(("http://", "https://")):
        footer = (
            f'<p style="margin:0 0 8px;font-family:{_FONT};font-size:13px;'
            f'line-height:1.5;color:{_TEXT};">Happy with our work? '
            f'<a href="{esc(review_url)}" style="color:{accent};font-weight:600;">'
            f"Leave us a Google review</a></p>{footer}"
        )

    pre = ""
    if preheader:
        # Hidden preview-pane text; kept out of the visible layout.
        pre = (
            f'<div style="display:none;max-height:0;overflow:hidden;'
            f'mso-hide:all;">{esc(preheader)}</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{esc(title) or company}</title>
</head>
<body style="margin:0;padding:0;background-color:#f1f5f9;">
{pre}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f1f5f9;">
  <tr>
    <td align="center" style="padding:24px 12px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background-color:#ffffff;border-radius:8px;">
        <tr>
          <td align="center" style="background-color:{accent};padding:20px 24px;border-radius:8px 8px 0 0;">{header_inner}</td>
        </tr>
        <tr>
          <td style="padding:24px;font-family:{_FONT};font-size:15px;line-height:1.55;color:{_TEXT};background-color:#ffffff;">
{body_html}
          </td>
        </tr>
        <tr>
          <td align="center" style="background-color:#f8fafc;padding:16px 24px;border-radius:0 0 8px 8px;font-family:{_FONT};font-size:12px;line-height:1.6;color:{_MUTED};">{footer}</td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""
