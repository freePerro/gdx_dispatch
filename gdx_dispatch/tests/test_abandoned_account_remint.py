"""No open intent keeps a customer's account once the pay page re-mints (GDXA-781).

`_create_usable_intent` cancels an abandoned intent only when the idempotency
key REPLAYS it, and the ACH key always replays the first mint. "Use a
different bank account" pressed twice therefore left the middle intent — with
the customer's second account attached, `requires_confirmation` — open until
the settle-time sweep: press 2 replayed the already-cancelled first intent and
never looked at it.

The stub below is stateful on purpose. A mock that returns canned objects
cannot reproduce this: the defect needs Stripe's documented replay behaviour
("the resulting status code and body of the first request",
<https://docs.stripe.com/api/idempotent_requests>) against an object whose
CURRENT state has moved on.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import secrets
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
import stripe

from gdx_dispatch.core.payments import CreateIntentRequest, create_intent
from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceLine

COMPANY = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    yield tenant_db


@pytest.fixture
def invoice(db):
    cust = Customer(id=uuid4(), name="Pay Page Customer", company_id=COMPANY)
    db.add(cust)
    db.flush()
    inv = Invoice(
        id=uuid4(), customer_id=cust.id, invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status="sent", subtotal=Decimal("200.00"), tax_amount=Decimal("0.00"),
        total=Decimal("200.00"), balance_due=Decimal("200.00"),
        invoice_date=dt.date(2026, 10, 12), public_token=secrets.token_urlsafe(48)[:64],
        company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(
        invoice_id=inv.id, description="Torsion spring", quantity=1,
        unit_price=Decimal("200.00"), line_total=Decimal("200.00"), company_id=COMPANY,
    ))
    db.commit()
    db.refresh(inv)
    return inv


class _FakeStripe:
    """Intents by id, with Stripe's replay-by-key and real state transitions."""

    def __init__(self):
        self.intents: dict[str, SimpleNamespace] = {}
        self.first_body: dict[str, SimpleNamespace] = {}
        self.cancelled: list[tuple[str, str]] = []
        self.fail_cancel: set[str] = set()

    def create(self, **kw):
        key = kw["idempotency_key"]
        if key in self.first_body:
            return copy.copy(self.first_body[key])
        pid = f"pi_{len(self.intents)}"
        pm = kw.get("payment_method")
        pi = SimpleNamespace(
            id=pid, status="requires_confirmation" if pm else "requires_payment_method",
            payment_method=pm, metadata=dict(kw["metadata"]), amount=kw["amount"],
            amount_received=0, client_secret=f"{pid}_secret", amount_details=None,
            payment_method_types=kw.get("payment_method_types", ["card"]), next_action=None,
            created=len(self.intents),
        )
        self.intents[pid] = pi
        self.first_body[key] = copy.copy(pi)
        return copy.copy(pi)

    def retrieve(self, pid, **_kw):
        return copy.copy(self.intents[pid])

    def cancel(self, pid, cancellation_reason=None, **_kw):
        pi = self.intents[pid]
        if pid in self.fail_cancel:
            raise stripe.APIConnectionError("network down")
        if pi.status in ("canceled", "succeeded"):
            raise stripe.InvalidRequestError(
                f"You cannot cancel this PaymentIntent because it has a status of {pi.status}.",
                param=None, code="payment_intent_unexpected_state",
            )
        pi.status = "canceled"
        self.cancelled.append((pid, cancellation_reason))
        return copy.copy(pi)

    def list(self, **_kw):
        return SimpleNamespace(data=[copy.copy(p) for p in self.intents.values()], has_more=False)

    def link_account(self, pid, pm):
        """What Stripe.js `collectBankAccountForPayment` does to the intent."""
        self.intents[pid].payment_method = pm
        self.intents[pid].status = "requires_confirmation"


@contextlib.contextmanager
def _stripe():
    fake = _FakeStripe()
    card = SimpleNamespace(card={"brand": "visa", "last4": "4242", "funding": "credit", "country": "US"})
    with patch("stripe.PaymentIntent.create", side_effect=fake.create), \
            patch("stripe.PaymentIntent.retrieve", side_effect=fake.retrieve), \
            patch("stripe.PaymentIntent.cancel", side_effect=fake.cancel), \
            patch("stripe.PaymentIntent.list", side_effect=fake.list), \
            patch("stripe.PaymentMethod.retrieve", return_value=card):
        yield fake


def _press(db, invoice, *, method="ach", payment_method_id=None):
    return create_intent(
        CreateIntentRequest(
            invoice_token=invoice.public_token, method=method, payment_method_id=payment_method_id,
        ),
        SimpleNamespace(state=SimpleNamespace(tenant={})),
        db=db,
    )["payment_intent_id"]


def test_a_second_different_account_press_cancels_the_middle_intent(db, invoice):
    """The defect. I1 carries account #2 and must not survive press 2."""
    with _stripe() as fake:
        i0 = _press(db, invoice)
        fake.link_account(i0, "pm_bank_1")
        i1 = _press(db, invoice)  # "Use a different bank account", press 1
        assert i1 != i0
        fake.link_account(i1, "pm_bank_2")
        i2 = _press(db, invoice)  # press 2

    assert i2 not in (i0, i1)
    assert fake.intents[i1].status == "canceled", "I1 still holds account #2"
    assert fake.intents[i2].status == "requires_payment_method"
    # Each abandoned intent cancelled exactly once, as abandoned: the sweep
    # runs before the mint, so the replay branch finds I0 already dead.
    assert fake.cancelled == [(i0, "abandoned"), (i1, "abandoned")]


def test_the_card_about_to_be_replayed_is_not_cancelled(db, invoice):
    """CONTROL. The two-step card flow mints with the card attached
    (`requires_confirmation` + PM by construction) and the Pay step re-POSTs
    the same card: that intent is the one handed back, and must stay live."""
    with _stripe() as fake:
        first = _press(db, invoice, method="card", payment_method_id="pm_card_a")
        second = _press(db, invoice, method="card", payment_method_id="pm_card_a")

    assert second == first
    assert fake.intents[first].status == "requires_confirmation"
    assert fake.cancelled == []


def test_switching_cards_leaves_the_card_intents_alone(db, invoice):
    """Card intents are deliberately out of the sweep: see the next test."""
    with _stripe() as fake:
        a = _press(db, invoice, method="card", payment_method_id="pm_card_a")
        b = _press(db, invoice, method="card", payment_method_id="pm_card_b")

    assert b != a
    assert fake.cancelled == []


def test_a_bank_press_from_another_tab_does_not_loop_the_card_page(db, invoice):
    """2026-10-12 audit. Cancelling the attached card intent here made its key
    replay a dead intent on every Pay click, each minting a new one under a
    random suffix, so the card page never settled ("The amount changed")."""
    with _stripe() as fake:
        step1 = _press(db, invoice, method="card", payment_method_id="pm_card_a")
        _press(db, invoice, method="ach")  # second tab
        pays = {_press(db, invoice, method="card", payment_method_id="pm_card_a") for _ in range(3)}

    assert pays == {step1}
    assert fake.cancelled == []


def test_an_intent_without_an_account_is_left_alone(db, invoice):
    """CONTROL. A reload before linking anything replays the same live intent."""
    with _stripe() as fake:
        first = _press(db, invoice)
        second = _press(db, invoice)

    assert second == first
    assert fake.cancelled == []


def test_a_failed_cancel_still_lets_the_customer_pay(db, invoice):
    """Best-effort: Stripe refusing the cleanup only logs; the mint goes on."""
    with _stripe() as fake:
        i0 = _press(db, invoice)
        fake.link_account(i0, "pm_bank_1")
        i1 = _press(db, invoice)
        fake.link_account(i1, "pm_bank_2")
        fake.fail_cancel.add(i1)
        i2 = _press(db, invoice)

        assert i2 not in (i0, i1)
        assert fake.intents[i2].status == "requires_payment_method"
        assert fake.intents[i1].status == "requires_confirmation"

        # Still in the register, so the next press tries it again.
        fake.fail_cancel.clear()
        _press(db, invoice)

    assert fake.intents[i1].status == "canceled"
