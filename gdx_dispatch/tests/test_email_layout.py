"""Unit tests for the shared email shell (core/email_layout.py).

These lock the email-client-compat invariants the shell exists to provide —
each maps to a failure documented in
docs/design/email-readability-and-delivery-plan.md. jsdom/pytest can't prove
rendering; what they CAN prove is that the constructions known to survive
Outlook/dark-mode are present and the known-broken ones are absent.
"""
from __future__ import annotations

import re

from gdx_dispatch.core import email_layout as el
from gdx_dispatch.core.email_sender import (
    build_estimate_email_html,
    build_invoice_email_html,
)

BRANDING = {
    "company_name": "Acme Door Co",
    "logo": "",
    "accent": "#0f766e",
    "phone": "(555) 100-2000",
    "address": "1 Main St, Springfield MN",
    "email": "office@acmedoor.example",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def test_esc_escapes_and_handles_none():
    assert el.esc('<b>&"x"</b>') == "&lt;b&gt;&amp;&quot;x&quot;&lt;/b&gt;"
    assert el.esc(None) == ""
    assert el.esc(5) == "5"


def test_nl2br_preserves_line_structure_and_escapes():
    out = el.nl2br("line one\nline <2>\r\nline three")
    assert out == "line one<br>line &lt;2&gt;<br>line three"


def test_to_plain_text_strips_markup_and_unescapes():
    html = "<p>Hi Bob,</p><p>Total: &amp; more<br>next line</p>"
    text = el.to_plain_text(html)
    assert "Hi Bob," in text
    assert "& more" in text
    assert "<" not in text


def test_to_plain_text_keeps_cta_urls_actionable():
    # Audit 2026-08-18 failure prediction: stripping <a> wholesale left the
    # text part with a call-to-action and no URL. Anchors become "label: url".
    html = build_invoice_email_html(
        company_name="Acme Door Co", invoice_number="INV-9", customer_name="Bob",
        line_items=[], subtotal=100, tax_amount=0, total=100, balance_due=100,
        portal_url="https://gdx.example.com/pay/tok?a=1&b=2", branding=BRANDING,
    )
    text = el.to_plain_text(html)
    assert "View & Pay Invoice: https://gdx.example.com/pay/tok?a=1&b=2" in text
    # <head> title + hidden preheader must not open the text part.
    assert not text.startswith("Invoice #INV-9\nInvoice #INV-9")


def test_cta_button_is_td_padded_not_anchor_padded():
    # Outlook ignores padding on <a>; the color+padding must live on the <td>.
    out = el.cta_button("https://x.example/pay", "Pay Now", "#123456")
    td = re.search(r"<td style=\"([^\"]+)\">", out)
    assert td and "background-color:#123456" in td.group(1)
    anchor_style = re.search(r"<a href=\"[^\"]+\" style=\"([^\"]+)\"", out)
    assert anchor_style and "display:inline-block" in anchor_style.group(1)
    assert el.cta_button("", "Pay Now") == ""


def test_cta_button_escapes_url_and_label():
    out = el.cta_button('https://x.example/?a=1&b="2"', "View & Accept", "#000")
    assert 'a=1&amp;b=' in out
    assert "View &amp; Accept" in out


def test_line_items_table_fonts_on_every_cell_and_multiline_descriptions():
    out = el.line_items_table(
        [{"description": "Torsion spring\n<heavy duty>", "quantity": 2,
          "unit_price": 1234.5, "line_total": 2469.0}]
    )
    # Every td/th carries its own font-family (Outlook doesn't inherit).
    for cell_style in re.findall(r"<t[dh][^>]*style=\"([^\"]+)\"", out):
        assert "font-family:" in cell_style
    assert "Torsion spring<br>&lt;heavy duty&gt;" in out
    assert "$1,234.50" in out and "$2,469.00" in out


# --------------------------------------------------------------------------- #
# shell
# --------------------------------------------------------------------------- #

def _shell(**kw):
    return el.render_email(
        branding=dict(BRANDING, **kw.pop("branding", {})),
        body_html=kw.pop("body_html", "<p>hello</p>"),
        **kw,
    )


def test_shell_is_table_based_with_width_attribute_and_style():
    html = _shell()
    # Outlook honors the width attribute; everyone else the inline style.
    assert re.search(r'<table role="presentation" width="600"[^>]*max-width:600px', html)
    assert "<body" in html and 'color-scheme" content="light"' in html


def test_shell_sets_explicit_backgrounds_everywhere():
    html = _shell()
    # Dark-mode survival: the text container must not be transparent.
    assert html.count("background-color:#ffffff") >= 2
    assert "background-color:#f1f5f9" in html


def test_shell_header_uses_accent_and_footer_carries_contact_identity():
    html = _shell()
    assert "background-color:#0f766e" in html
    assert "(555) 100-2000" in html
    assert "1 Main St, Springfield MN" in html
    assert "office@acmedoor.example" in html
    # The old hardcoded platform blues are gone.
    assert "#0057a8" not in html


def test_shell_branding_fallbacks_never_break_the_header():
    html = el.render_email(
        branding={"company_name": "", "logo": "", "accent": "", "phone": "",
                  "address": "", "email": ""},
        body_html="<p>x</p>",
    )
    assert "Your Service Company" in html
    assert el.DEFAULT_ACCENT in html


def test_shell_escapes_company_name():
    html = _shell(branding={"company_name": 'A<script>"co"'})
    assert "<script>" not in html


def test_absolute_logo_url_rules(monkeypatch):
    assert el._absolute_logo_url("https://cdn.example/logo.png") == "https://cdn.example/logo.png"
    monkeypatch.delenv("GDX_PUBLIC_BASE_URL", raising=False)
    # Relative path with no public base → dropped (text header, not broken img).
    assert el._absolute_logo_url("/static/logo.png") == ""
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://gdx.example.com/")
    assert el._absolute_logo_url("/static/logo.png") == "https://gdx.example.com/static/logo.png"


# --------------------------------------------------------------------------- #
# builders on the shell
# --------------------------------------------------------------------------- #

def _estimate(**kw):
    defaults = dict(
        company_name="Acme Door Co",
        estimate_number="EST-1",
        customer_name="Bob",
        line_items=[{"description": "FlatLineItemXYZ", "quantity": 1,
                     "unit_price": 100.0, "line_total": 100.0}],
        total=100.0,
        branding=BRANDING,
    )
    defaults.update(kw)
    return build_estimate_email_html(**defaults)


def test_estimate_email_no_more_hardcoded_30_day_lie():
    html = _estimate()
    assert "valid for 30 days" not in html
    html2 = _estimate(valid_until_text="October 17, 2026")
    assert "valid until October 17, 2026" in html2


def test_estimate_email_escapes_customer_and_notes():
    html = _estimate(customer_name='Bob <"Big"> & Sons', notes="line1\nline2")
    assert 'Bob &lt;&quot;Big&quot;&gt; &amp; Sons' in html
    assert "line1<br>line2" in html


def test_estimate_tier_mode_suppresses_flat_lines_and_single_total():
    html = _estimate(
        tiers=[{"name": "Good", "price": 1000, "description": "Base door"},
               {"name": "Better", "price": 1500.5, "description": ""}],
        portal_url="https://gdx.example.com/proposals/tok",
    )
    assert "Good" in html and "$1,500.50" in html
    # No flat line table, no single Total row in tier mode.
    assert "FlatLineItemXYZ" not in html
    assert ">Total<" not in html
    assert "View Options &amp; Choose Online" in html


def test_invoice_email_totals_foot_and_use_thousands_separators():
    html = build_invoice_email_html(
        company_name="Acme Door Co",
        invoice_number="INV-9",
        customer_name="Bob",
        line_items=[],
        subtotal=12345.67,
        tax_amount=0,
        total=12345.67,
        balance_due=2345.67,
        paid_to_date=10000.0,
        branding=BRANDING,
    )
    assert "$12,345.67" in html
    assert "-$10,000.00" in html
    assert "$2,345.67" in html


def test_invoice_receipt_flavor():
    html = build_invoice_email_html(
        company_name="Acme Door Co",
        invoice_number="INV-9",
        customer_name="Bob",
        line_items=[],
        subtotal=100, tax_amount=0, total=100, balance_due=0,
        portal_url="https://x.example/pay",
        branding=BRANDING,
        is_receipt=True,
    )
    assert "Payment received" in html
    # A paid invoice gets a thank-you, not a Pay button or a due date.
    assert "Pay Invoice" not in html
