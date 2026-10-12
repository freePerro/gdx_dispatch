"""A replayed key whose intent is dead must converge on one intent (GDXA-784).

`_create_usable_intent` used to re-mint a dead replay under a random
`-r<uuid>` key. The next press replayed the same dead intent and minted
another, so after the sweep cancelled a card intent every Pay press made a new
intent and the page answered "The amount changed since you last looked"
forever. The fix reuses a live intent already open on the invoice, and
otherwise keys the re-mint on the dead intent's id so the next press can find
it again.

The stub is stateful on purpose (carried from GDXA-781's
test_abandoned_account_remint.py). A mock that returns canned objects cannot
reproduce this: the defect needs Stripe's documented replay behaviour ("the
resulting status code and body of the first request",
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
        self.keys: list[str] = []
        self.cancelled: list[tuple[str, str]] = []

    def _new(self, *, amount, metadata, pm=None, types=None, status=None):
        pid = f"pi_{len(self.intents)}"
        pi = SimpleNamespace(
            id=pid, status=status or ("requires_confirmation" if pm else "requires_payment_method"),
            payment_method=pm, metadata=dict(metadata), amount=amount,
            amount_received=0, client_secret=f"{pid}_secret", amount_details=None,
            payment_method_types=types or ["card"], next_action=None,
            created=len(self.intents),
        )
        self.intents[pid] = pi
        return pi

    def create(self, **kw):
        key = kw["idempotency_key"]
        self.keys.append(key)
        if key in self.first_body:
            return copy.copy(self.first_body[key])
        pi = self._new(
            amount=kw["amount"], metadata=kw["metadata"], pm=kw.get("payment_method"),
            types=kw.get("payment_method_types"),
        )
        self.first_body[key] = copy.copy(pi)
        return copy.copy(pi)

    def retrieve(self, pid, **_kw):
        return copy.copy(self.intents[pid])

    def cancel(self, pid, cancellation_reason=None, **_kw):
        pi = self.intents[pid]
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

    def sweep(self, pid):
        """What `cancel_open_intents_for_invoice` does when a payment is
        recorded and then voided: the intent dies, the balance returns."""
        self.intents[pid].status = "canceled"

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
    )


def test_three_card_presses_after_the_sweep_converge_on_one_intent(db, invoice):
    """The defect. On main: three presses, three intents."""
    with _stripe() as fake:
        first = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]
        fake.sweep(first)
        pays = [_press(db, invoice, method="card", payment_method_id="pm_1") for _ in range(3)]

    ids = {p["payment_intent_id"] for p in pays}
    assert len(ids) == 1, f"every press minted another intent: {ids}"
    (live,) = ids
    assert live != first
    assert fake.intents[live].status == "requires_confirmation"
    assert fake.intents[live].payment_method == "pm_1"
    assert len(fake.intents) == 2, "one dead intent and one live one, nothing else"


def test_bank_presses_after_the_sweep_converge_too(db, invoice):
    """The ACH rail runs through the same branch."""
    with _stripe() as fake:
        first = _press(db, invoice)["payment_intent_id"]
        fake.sweep(first)
        ids = {_press(db, invoice)["payment_intent_id"] for _ in range(3)}

    assert len(ids) == 1
    assert fake.intents[ids.pop()].status == "requires_payment_method"
    assert len(fake.intents) == 2


def test_the_chain_walks_on_when_the_re_mint_dies_too(db, invoice):
    """The sweep kills the re-mint as well: the next press replays the dead
    re-mint by its own key and walks one hop further, not back to random."""
    with _stripe() as fake:
        i0 = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]
        fake.sweep(i0)
        i1 = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]
        fake.sweep(i1)
        ids = {_press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"] for _ in range(3)}

    assert len(ids) == 1
    assert ids.isdisjoint({i0, i1})
    assert len(fake.intents) == 3
    remints = [k for k in fake.keys if "-r" in k]
    assert remints and all(k.rsplit("-r", 1)[1].startswith("pi_") for k in remints), \
        f"a one-off random key was used: {remints}"


def test_an_intent_already_open_for_the_same_card_is_reused(db, invoice):
    """A live intent minted under a key this press cannot name (a pre-fix
    `-r<uuid>` one, already sized with the card fee) is handed back rather
    than duplicated. The fee in its metadata is not part of the amount owed."""
    with _stripe() as fake:
        i0 = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]
        fake.sweep(i0)
        legacy = fake._new(
            amount=20600, metadata={"invoice_id": str(invoice.id), "surcharge_cents": "600"}, pm="pm_1",
        )
        out = _press(db, invoice, method="card", payment_method_id="pm_1")

    assert out["payment_intent_id"] == legacy.id
    assert out["amount"] == 20600 and out["surcharge_cents"] == 600
    assert len(fake.intents) == 2, "a duplicate intent was minted beside the open one"


def test_an_open_intent_on_another_card_or_amount_is_not_reused(db, invoice):
    """CONTROL. Reuse needs the same PaymentMethod and the same amount owed."""
    with _stripe() as fake:
        i0 = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]
        fake.sweep(i0)
        other_card = fake._new(amount=20000, metadata={"invoice_id": str(invoice.id)}, pm="pm_2")
        old_amount = fake._new(amount=25000, metadata={"invoice_id": str(invoice.id)}, pm="pm_1")
        got = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]

    assert got not in (i0, other_card.id, old_amount.id)
    assert fake.intents[other_card.id].status == "requires_confirmation"


def test_a_fee_bearing_intent_is_not_reused_by_a_press_without_a_card(db, invoice):
    """2026-10-12 audit. A one-step card press (no card attached: an old tab)
    is never charged a fee, so an open intent whose amount carries one must not
    be handed to it — the page would show 200.00 and Stripe charge 206.00."""
    with _stripe() as fake:
        i0 = _press(db, invoice, method="card")["payment_intent_id"]
        fake.sweep(i0)
        sized = fake._new(
            amount=20600, metadata={"invoice_id": str(invoice.id), "surcharge_cents": "600"},
            status="requires_payment_method",
        )
        out = _press(db, invoice, method="card")

    assert out["payment_intent_id"] != sized.id
    assert fake.intents[out["payment_intent_id"]].amount == out["amount"] == 20000


def test_an_exhausted_chain_still_hands_back_a_live_intent(db, invoice):
    """The sweep killed every hop the chain can walk: the last resort is a
    one-off key — logged, and still an intent the customer can pay."""
    from gdx_dispatch.core.payments import _REMINT_MAX_HOPS

    with _stripe() as fake:
        for _ in range(_REMINT_MAX_HOPS + 1):
            fake.sweep(_press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"])
        dead = set(fake.intents)
        out = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]

    assert out not in dead
    assert fake.intents[out].status == "requires_confirmation"
    assert not fake.keys[-1].rsplit("-r", 1)[1].startswith("pi_")


def test_a_second_different_account_press_cancels_the_middle_intent(db, invoice):
    """The ACH instance of the class (GDXA-781's first test). Press 2 replays
    the cancelled first intent; the deterministic re-mint key then replays the
    middle intent, which still holds account #2, so it is cancelled as
    abandoned rather than left open — and never handed out as a candidate."""
    with _stripe() as fake:
        i0 = _press(db, invoice)["payment_intent_id"]
        fake.link_account(i0, "pm_bank_1")
        i1 = _press(db, invoice)["payment_intent_id"]
        assert i1 != i0
        fake.link_account(i1, "pm_bank_2")
        i2 = _press(db, invoice)["payment_intent_id"]

    assert i2 not in (i0, i1)
    assert fake.intents[i1].status == "canceled", "I1 still holds account #2"
    assert fake.intents[i2].status == "requires_payment_method"
    assert fake.cancelled == [(i0, "abandoned"), (i1, "abandoned")]


def test_a_live_replay_mints_nothing(db, invoice):
    """CONTROL. The happy path is unchanged: same intent, one create each."""
    with _stripe() as fake:
        a = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]
        b = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]

    assert a == b
    assert len(fake.keys) == 2 and len(set(fake.keys)) == 1
    assert fake.cancelled == []
