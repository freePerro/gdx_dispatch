"""The pay page offers no payment form when nothing is owed (GDXA-571).

`payment_form.html` decided "nothing to pay" on `invoice.status == "paid"`
alone. An invoice whose balance reached zero without its status moving — the
office recorded the payment, a credit covered it — rendered a live
"Pay $0.00" form with Stripe Elements mounted, and the first click got
`create-intent`'s 409 "This invoice has no balance due."
(`core/payments.py`, `_resolve_public_invoice`). A customer-facing dead end.

These render the real template through the app's own Jinja environment and
assert on the output, not the template source.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

SETTLED_BANNER = "This invoice has already been paid."
STRIPE_SETUP = "const stripe = Stripe("


class _Invoice:
    invoice_number = "INV-0001"
    public_token = "tok_test"

    def __init__(self, status: str, balance_due):
        self.status = status
        self.balance_due = balance_due


def _render(status: str, balance_due) -> str:
    from gdx_dispatch.core.payments import templates

    return templates.get_template("payment_form.html").render(
        invoice=_Invoice(status, balance_due),
        ach_processing=None,
        stripe_publishable_key="pk_test",
        company_name="Test Co",
        surcharge_rate=0.0,
        surcharge_percent_label="",
        job_photos=[],
    )


@pytest.mark.parametrize(
    ("status", "balance_due"),
    [
        ("sent", 0),
        ("sent", 0.0),
        ("sent", Decimal("0.00")),
        ("partial", 0),
        ("overdue", 0),
        ("sent", -5.00),  # overpaid: still nothing to charge
        ("sent", None),
        ("paid", 0),
        ("paid", 12.50),  # status paid wins even if a stale balance lingers
    ],
)
def test_nothing_owed_renders_settled_with_no_payment_form(status, balance_due):
    html = _render(status, balance_due)
    assert SETTLED_BANNER in html
    assert "Pay $0.00" not in html
    assert STRIPE_SETUP not in html
    assert 'id="card-form"' not in html
    assert 'id="invoice-total"' not in html


@pytest.mark.parametrize("status", ["sent", "partial", "overdue"])
def test_a_balance_still_owed_renders_the_live_form(status):
    html = _render(status, 200.00)
    assert SETTLED_BANNER not in html
    assert STRIPE_SETUP in html
    assert 'id="card-form"' in html
    assert "$200.00" in html


def test_a_void_invoice_with_no_balance_is_never_called_paid():
    """Void is not money the customer paid; the settled banner would be false."""
    html = _render("void", 0)
    assert SETTLED_BANNER not in html
