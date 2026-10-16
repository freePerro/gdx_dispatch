"""Customer-facing money on the pay page and install sheet groups thousands
(GDXA-1294).

Both templates formatted with ``"%.2f"|format(...)``, so a balance of 1800
rendered "$1800.00" — and on the pay page the same amount flipped to
"$1,800.00" once the card intent came back, because the page's own JS
``money()`` uses ``toLocaleString``. The shared shape is
``core/money_format.format_money``: comma grouping, sign outside the dollar.

These render the real templates through the environments the routes use and
assert on the output, so a value below 1,000 (where both shapes agree) cannot
hide the defect.
"""
from __future__ import annotations

import re
from decimal import Decimal
from types import SimpleNamespace

import pytest


def _render_pay_page(balance_due, card_two_step: bool) -> str:
    from gdx_dispatch.core.payments import templates

    return templates.get_template("payment_form.html").render(
        invoice=SimpleNamespace(invoice_number="INV-0001", public_token="tok_test", status="sent", balance_due=balance_due),
        ach_processing=None,
        stripe_publishable_key="pk_test",
        company_name="Test Co",
        surcharge_rate=0.0,
        surcharge_percent_label="",
        job_photos=[],
        card_two_step=card_two_step,
    )


def _render_install_sheet(parts, estimate_total, hide=False) -> str:
    from gdx_dispatch.routers.install_sheet import _load_template

    return _load_template().render(
        company_name="GDX", job_number="JOB-0001", customer_name="Jane",
        address="", phone="", scheduled_at="", technician="Bob",
        job_type="Install", priority="Normal", door_specs=None,
        parts=parts, estimate_total=estimate_total, hide_line_prices=hide,
        notes="", auto_print=False,
    )


@pytest.mark.parametrize("balance_due", [1800, 1800.0, Decimal("1800.00")])
def test_pay_page_two_step_groups_thousands_in_every_server_rendered_amount(balance_due):
    """A surcharge or debit refusal makes the card flow two-step: the total,
    the confirm panel's invoice line (the one the JS later rewrites with
    commas) and the confirm button all carry the balance."""
    html = _render_pay_page(balance_due, card_two_step=True)
    assert "1800.00" not in html
    total = re.search(r'id="invoice-total">([^<]*)<', html)
    card_amount = re.search(r'id="card-invoice-amount">([^<]*)<', html)
    assert total and total.group(1) == "$1,800.00"
    assert card_amount and card_amount.group(1) == "$1,800.00"
    assert "Confirm and pay $1,800.00" in html


def test_pay_page_one_step_button_groups_thousands():
    html = _render_pay_page(1234567.5, card_two_step=False)
    assert "Pay $1,234,567.50" in html
    total = re.search(r'id="invoice-total">([^<]*)<', html)
    assert total and total.group(1) == "$1,234,567.50"
    assert "1234567.50" not in html


def test_install_sheet_groups_thousands_and_puts_the_sign_outside():
    parts = [
        {"description": "Double door", "category": "Doors", "quantity": 1, "unit_price": 1800.0, "total": 1800.0},
        {"description": "Spring kit", "category": "Springs", "quantity": 2, "unit_price": 612.5, "total": 1225.0},
        {"description": "Loyalty discount", "category": "Discount", "quantity": 1, "unit_price": -1050.0, "total": -1050.0},
    ]
    html = _render_install_sheet(parts, 1975.0)
    for text in ("$1,800.00", "$612.50", "$1,225.00", "-$1,050.00"):
        assert text in html
    assert "$-" not in html
    assert "1800.00" not in html
    total = re.search(r"Estimate Total</td><td[^>]*>([^<]*)<", html)
    assert total and total.group(1) == "$1,975.00"


def test_install_sheet_total_groups_thousands_when_line_prices_are_hidden():
    parts = [{"description": "Double door", "category": "Doors", "quantity": 1, "unit_price": 1800.0, "total": 1800.0}]
    html = _render_install_sheet(parts, 1800.0, hide=True)
    assert "$1,800.00" in html
    assert "1800.00" not in html
