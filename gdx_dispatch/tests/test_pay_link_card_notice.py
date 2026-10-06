"""The card-fee sentence rides every customer email that carries a pay link
(GDXA-252).

The invoice email has said it since the surcharge shipped; the payment
reminder and the customer statement carried the same /pay link with no word
about what a card costs. The pay page holds the statutory notice, so this is
a courtesy, not compliance: nobody should click through to a fee the email
did not mention. Off means not a word about fees, and no link means no
sentence.

The rate lookup is patched (its real SQLite read is pinned in
test_payments.py); `card_surcharge_notice` and both senders run for real.
"""
from __future__ import annotations

import secrets
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest

from gdx_dispatch.models.tenant_models import Customer, Invoice, ReminderSettings

COMPANY = "11111111-1111-1111-1111-111111111111"
FEE = "Credit cards carry a 2.9% processing fee."


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://pay.example.invalid")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "not-a-real-key-presence-only")
    return tenant_db


def _seed(db) -> tuple[Customer, Invoice]:
    c = Customer(name="Fee Customer", email="fee@example.com", company_id=COMPANY)
    db.add(c)
    db.commit()
    db.refresh(c)
    inv = Invoice(
        id=uuid4(),
        customer_id=c.id,
        invoice_number=f"INV-{uuid4().hex[:6].upper()}",
        billing_type="standard",
        status="sent",
        subtotal=Decimal("640"),
        tax_amount=Decimal("0"),
        total=Decimal("640"),
        balance_due=Decimal("640"),
        invoice_date=date(2026, 8, 1),
        due_date=date(2026, 8, 31),
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=COMPANY,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return c, inv


def _send_reminder(db, inv, rate: str) -> str:
    from gdx_dispatch.routers.invoice_reminders import send_reminder_email_for_invoice

    captured: dict[str, str] = {}

    def fake_send(**kwargs):
        captured["html"] = kwargs["html_body"]
        return True, "test", None

    settings = ReminderSettings(company_id=COMPANY)
    settings.subject_template = "Payment reminder for invoice {invoice_number}"
    settings.body_template = "Hi {customer_name}, invoice {invoice_number} is past due."
    with patch("gdx_dispatch.core.payments.card_surcharge_rate", return_value=Decimal(rate)), \
            patch("gdx_dispatch.core.transactional_email.send_transactional_email", side_effect=fake_send):
        sent, skip = send_reminder_email_for_invoice(db, COMPANY, inv, settings, user_id="office-user")
    assert (sent, skip) == (True, None)
    return captured["html"]


def test_reminder_email_carries_the_fee_sentence_beside_its_pay_button(db):
    _c, inv = _seed(db)
    on = _send_reminder(db, inv, "0.029")
    assert FEE in on and "bank transfer (ACH): no fee" in on
    assert f"/pay/{inv.public_token}" in on, "the button still renders"
    assert on.index(FEE) < on.index(f"/pay/{inv.public_token}"), "the sentence comes before the button"

    off = _send_reminder(db, inv, "0")
    assert "processing fee" not in off
    assert f"/pay/{inv.public_token}" in off


def test_reminder_with_no_pay_link_says_nothing_about_fees(db, monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    _c, inv = _seed(db)
    html = _send_reminder(db, inv, "0.029")
    assert "/pay/" not in html and "processing fee" not in html


def test_statement_email_and_pdf_carry_the_fee_sentence_with_their_pay_links(db):
    from gdx_dispatch.core.pdf_generator import _JINJA_ENV, _default_branding
    from gdx_dispatch.routers.customer_statements import _email_html
    from gdx_dispatch.routers.pdf import _branding_payload
    from gdx_dispatch.services.customer_statements import (
        KIND_LABELS,
        StatementRange,
        build_statement,
        to_json,
    )

    c, inv = _seed(db)
    rng = StatementRange(start=date(2026, 8, 1), end=date(2026, 9, 15), preset="custom")

    def render(rate: str) -> tuple[str, str]:
        with patch("gdx_dispatch.core.payments.card_surcharge_rate", return_value=Decimal(rate)):
            s = build_statement(db, c, rng, today=date(2026, 9, 15))
        page = _JINJA_ENV.get_template("statement_pdf.html").render(
            s=s, branding=_default_branding(_branding_payload(db)), kind_labels=KIND_LABELS,
        )
        return _email_html(db, to_json(s), "Pat")[1], page

    email, page = render("0.029")
    for out in (email, page):
        assert FEE in out
        assert f"/pay/{inv.public_token}" in out

    email, page = render("0")
    for out in (email, page):
        assert "processing fee" not in out
        assert f"/pay/{inv.public_token}" in out


def test_statement_with_no_pay_link_says_nothing_about_fees(db, monkeypatch):
    from gdx_dispatch.routers.customer_statements import _email_html
    from gdx_dispatch.services.customer_statements import StatementRange, build_statement, to_json

    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    c, _inv = _seed(db)
    rng = StatementRange(start=date(2026, 8, 1), end=date(2026, 9, 15), preset="custom")
    with patch("gdx_dispatch.core.payments.card_surcharge_rate", return_value=Decimal("0.029")):
        s = build_statement(db, c, rng, today=date(2026, 9, 15))
    assert s["card_notice"] == ""
    assert "processing fee" not in _email_html(db, to_json(s), "Pat")[1]
