"""A Stripe payment must ring the office bell.

Every surface that records processor money — the emailed pay page's
`/confirm`, the portal charge, the ACH charge, and the signed webhook that
settles a bank debit one to two business days later — funnels through
`_mark_invoice_paid`. Until 2026-08-26 that function wrote a `Payment` row, a
ledger entry and an audit trail and rang **nothing**: prod carried five card
payments and a `notifications` table holding only `lead` and `estimate` rows.
The office learned a customer had paid by reopening the invoice.

The bell row is broadcast (`user_id IS NULL`) so every office user sees it,
exactly like the landing-lead and estimate-decision alerts it copies.

What these lock:
  - a recorded payment writes one `payment` notification naming who, how
    much, which invoice, and by what method;
  - a partial payment says what is still due (a bell that implies "settled"
    on a half payment is worse than none);
  - **it rings exactly once** — the webhook and `/confirm` race by design and
    both call this, so a redelivery must not double-ring;
  - an overcharge says so (`balance_due` clamps at zero, so a stale pay page
    collecting more than the invoice owes otherwise rings like a clean
    settlement);
  - a broken notification path can never cost the payment — including a
    database failure on the expired-instance refresh, which is the raise that
    would escape into `/confirm`, the ACH charge and the webhook, none of
    which wrap this call.
"""
from __future__ import annotations

import datetime as dt
import secrets
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from gdx_dispatch.core.payments import _mark_invoice_paid
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceLine,
    Notification,
    Payment,
)

COMPANY = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    return tenant_db


def _invoice(db, total="1000.00", customer_name="Jane Doe"):
    cust = Customer(id=uuid4(), name=customer_name, company_id=COMPANY)
    db.add(cust)
    db.flush()
    inv = Invoice(
        id=uuid4(),
        customer_id=cust.id,
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status="sent",
        subtotal=Decimal(total),
        tax_amount=Decimal("0.00"),
        total=Decimal(total),
        balance_due=Decimal(total),
        invoice_date=dt.date(2026, 8, 26),
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=inv.id, description="Door service", quantity=1,
            unit_price=Decimal(total), line_total=Decimal(total), company_id=COMPANY,
        )
    )
    db.commit()
    db.refresh(inv)
    return inv


def _bell_rows(db):
    return db.execute(
        select(Notification).where(
            Notification.tenant_id == COMPANY,
            Notification.category == "payment",
        )
    ).scalars().all()


def test_card_payment_rings_the_office_bell(db):
    inv = _invoice(db, total="500.00")

    _mark_invoice_paid(
        inv, db, external_ref="pi_bell_card", method="card",
        amount=500.0, source="stripe-webhook",
    )

    rows = _bell_rows(db)
    assert len(rows) == 1, "a Stripe payment must write exactly one bell row"
    row = rows[0]
    assert row.title == "Payment received"
    assert row.user_id is None, "must be a tenant broadcast, not addressed to one user"
    assert row.is_read == 0
    assert row.deleted_at is None
    # Who, how much, which invoice, by what.
    assert "Jane Doe" in row.message
    assert "$500.00" in row.message
    assert inv.invoice_number in row.message
    assert "by card" in row.message
    # Fully settled — nothing should imply money is still outstanding.
    assert "still due" not in row.message


def test_ach_payment_says_bank_transfer_not_the_stripe_code(db):
    inv = _invoice(db, total="250.00", customer_name="Acme Storage LLC")

    _mark_invoice_paid(
        inv, db, external_ref="pi_bell_ach", method="ach",
        amount=250.0, source="stripe-webhook",
    )

    (row,) = _bell_rows(db)
    assert "by bank transfer" in row.message
    assert "by ach" not in row.message.lower()
    assert "Acme Storage LLC" in row.message


def test_partial_payment_names_the_remaining_balance(db):
    inv = _invoice(db, total="1000.00")

    _mark_invoice_paid(
        inv, db, external_ref="pi_bell_partial", method="card",
        amount=400.0, source="stripe-confirm",
    )

    (row,) = _bell_rows(db)
    assert "$400.00" in row.message
    assert "$600.00 still due" in row.message, (
        "a partial payment that reads as settled sends the office to stop chasing "
        f"a balance that is still open: {row.message}"
    )


def test_webhook_redelivery_rings_exactly_once(db):
    """`/confirm` and the signed webhook race by design; both land here."""
    inv = _invoice(db, total="500.00")

    _mark_invoice_paid(
        inv, db, external_ref="pi_bell_once", method="card",
        amount=500.0, source="stripe-confirm",
    )
    _mark_invoice_paid(
        inv, db, external_ref="pi_bell_once", method="card",
        amount=500.0, source="stripe-webhook",
    )

    assert len(_bell_rows(db)) == 1
    payments = db.execute(
        select(Payment).where(Payment.invoice_id == inv.id)
    ).scalars().all()
    assert len(payments) == 1


def test_zero_amount_trueup_does_not_ring(db):
    """No new money, no ping. A fully-paid invoice re-confirmed is not news."""
    inv = _invoice(db, total="500.00")
    _mark_invoice_paid(
        inv, db, external_ref="pi_first", method="card",
        amount=500.0, source="stripe-webhook",
    )
    assert len(_bell_rows(db)) == 1

    # Different reference (so the idempotency check passes) but nothing left
    # to collect — _mark_invoice_paid takes the pay_amount <= 0 early return.
    _mark_invoice_paid(
        inv, db, external_ref="pi_second", method="card",
        amount=0.0, source="stripe-webhook",
    )
    assert len(_bell_rows(db)) == 1


def test_a_broken_bell_never_costs_the_payment(db, monkeypatch):
    """The money is committed before this runs and must stay committed.

    Counterfactual for the never-raises contract: without it, a notification
    bug becomes a payment bug — the class this repo ranks highest.
    """
    inv = _invoice(db, total="500.00")

    import gdx_dispatch.core.office_notifications as office

    def _boom(*_a, **_kw):
        raise RuntimeError("notification backend exploded")

    monkeypatch.setattr(office, "notify_office", _boom)

    _mark_invoice_paid(
        inv, db, external_ref="pi_bell_boom", method="card",
        amount=500.0, source="stripe-webhook",
    )

    payments = db.execute(
        select(Payment).where(
            Payment.invoice_id == inv.id, Payment.voided_at.is_(None)
        )
    ).scalars().all()
    assert len(payments) == 1, "the payment must survive a failed bell write"
    assert float(payments[0].amount) == 500.0
    assert _bell_rows(db) == []


def test_a_failed_post_commit_read_cannot_escape_into_the_caller(db, monkeypatch):
    """The raise the adversarial audit caught, as a test.

    `db.commit()` expires the invoice, so every attribute read after it is a
    lazy SELECT that a dropped connection turns into an `OperationalError`.
    Three of the four call sites — `/confirm`, the ACH charge, the webhook —
    do not wrap `_mark_invoice_paid`, so a raise there 500s a request whose
    money is already committed: the pay page telling a customer their good
    card charge failed, and on the webhook a Stripe retry that takes the
    idempotent early return and loses the bell for good.

    This is the counterfactual for "read the tenant id INSIDE the guard".
    The failure is armed by an `after_commit` listener, so `company_id` reads
    succeed for everything `_mark_invoice_paid` does BEFORE the money lands
    (building the Payment row, the ledger repost) and start failing the moment
    it is committed — exactly the shape of a connection dropped mid-request.
    With the read inside the guard this is swallowed; with it back on the
    caller's line the exception escapes and this test goes red.
    """
    inv = _invoice(db, total="500.00")

    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    state = {"armed": False, "reads_after_commit": 0}

    def _arm(_session):
        state["armed"] = True

    event.listen(db, "after_commit", _arm)

    real = Invoice.company_id

    def _flaky(self):
        if state["armed"]:
            state["reads_after_commit"] += 1
            raise OperationalError("SELECT invoices...", {}, Exception("server closed"))
        return COMPANY

    monkeypatch.setattr(Invoice, "company_id", property(_flaky), raising=False)
    try:
        # Must NOT raise.
        _mark_invoice_paid(
            inv, db, external_ref="pi_read_boom", method="card",
            amount=500.0, source="stripe-confirm",
        )
    finally:
        monkeypatch.setattr(Invoice, "company_id", real, raising=False)
        event.remove(db, "after_commit", _arm)

    assert state["reads_after_commit"] > 0, (
        "no post-commit read happened — this test would pass vacuously"
    )
    db.rollback()
    payments = db.execute(
        select(Payment).where(
            Payment.invoice_id == inv.id, Payment.voided_at.is_(None)
        )
    ).scalars().all()
    assert len(payments) == 1, "the money must survive a failed post-commit read"
    assert float(payments[0].amount) == 500.0
    assert _bell_rows(db) == []




def test_an_overcharge_does_not_ring_like_a_clean_settlement(db):
    """`balance_due` is clamped at 0, so the invoice's own columns hide this.

    A PaymentIntent freezes its amount at mint time, so a tab left open on a
    $500 invoice can still collect $500 after the office banks a $300 check.
    The money is recorded (it moved); the office has to hear that $300 of it
    is a customer credit, not a payment.
    """
    inv = _invoice(db, total="500.00")
    _mark_invoice_paid(
        inv, db, external_ref="pi_first_half", method="card",
        amount=200.0, source="stripe-webhook",
    )
    _mark_invoice_paid(
        inv, db, external_ref="pi_stale_tab", method="card",
        amount=500.0, source="stripe-confirm",
    )

    latest = _bell_rows(db)[-1]
    assert "$200.00 MORE than owed" in latest.message, latest.message
    assert "still due" not in latest.message
    db.refresh(inv)
    assert float(inv.balance_due) == 0.0, (
        "if balance_due ever stops clamping, the overpaid branch is unreachable "
        "and this test is what tells you"
    )


def test_an_invoice_with_no_tenant_writes_nothing_rather_than_an_invisible_row(db):
    """`tenant_id=""` matches no bell query — that is a silent no-op, not an alert.

    The bell filters `Notification.tenant_id == request.state.tenant["id"]`, so
    a row filed under the empty string is written, counted as success, and
    seen by nobody. Refusing loudly beats that.
    """
    inv = _invoice(db, total="500.00")

    from gdx_dispatch.core import office_notifications as office

    office.notify_payment_received(db, inv, amount=500.0, method="card")
    assert len(_bell_rows(db)) == 1  # sanity: the real tenant does ring

    inv.company_id = ""
    office.notify_payment_received(db, inv, amount=500.0, method="card")

    blank = db.execute(
        select(Notification).where(Notification.tenant_id == "")
    ).scalars().all()
    assert blank == [], "an invisible notification is worse than none"
    assert len(_bell_rows(db)) == 1
