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
    # A pm_1 press that is not starting pm_1 sweeps no card (GDXA-790).
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


def test_use_a_different_card_cancels_the_first_cards_intent(db, invoice):
    """GDXA-790. pm_2 mints under its own key beside pm_1's intent, which
    used to stay `requires_confirmation` with pm_1 attached until the
    settle-time sweep. The pm_2 press cancels it, once, as abandoned."""
    with _stripe() as fake:
        one = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]
        two = _press(db, invoice, method="card", payment_method_id="pm_2")
        pay = _press(db, invoice, method="card", payment_method_id="pm_2")  # the Pay step

    assert fake.intents[one].status == "canceled"
    assert fake.cancelled == [(one, "abandoned")]
    assert pay["payment_intent_id"] == two["payment_intent_id"] != one
    assert fake.intents[two["payment_intent_id"]].status == "requires_confirmation"
    assert fake.intents[two["payment_intent_id"]].payment_method == "pm_2"


def test_a_card_switch_cancel_records_its_row_on_the_invoice(db, invoice, caplog):
    """Invariant #1 (GDXA-787 applied to GDXA-790): the card sweep's cancel is
    a money-object change like the bank sweep's, so it leaves the same row,
    told apart by ``why``. Read back what is COMMITTED, not what is staged."""
    from gdx_dispatch.core.audit import AuditLog

    with _stripe(), caplog.at_level("WARNING"):
        one = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]
        _press(db, invoice, method="card", payment_method_id="pm_2")

    assert "audit_best_effort_caller_has_pending_work" not in caplog.text
    db.rollback()
    rows = db.query(AuditLog).filter(AuditLog.action == "abandoned_account_intent_canceled").all()
    assert len(rows) == 1
    assert rows[0].user_id == "stripe-pay-page"
    assert str(rows[0].entity_id) == str(invoice.id)
    assert rows[0].details == {"intent_id": one, "why": "card-switch", "status": "canceled"}


def test_pm_1_pressed_again_after_the_switch_converges(db, invoice):
    """pm_1 again after switching. The page cannot do this from one tab ("Use
    a different card" drops pm_1, and step 1 always creates a new card); a
    second tab still on pm_1's Pay step can. Its key replays the intent pm_2
    cancelled, so it re-mints under `{key}-r{dead id}` and every later pm_1
    press lands on that one intent. It is not starting a card, so pm_2's
    intent stays live."""
    with _stripe() as fake:
        one = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]
        two = _press(db, invoice, method="card", payment_method_id="pm_2")["payment_intent_id"]
        ids = {_press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"] for _ in range(3)}

    assert len(ids) == 1, f"every press minted another intent: {ids}"
    (live,) = ids
    assert live not in (one, two)
    assert fake.intents[live].status == "requires_confirmation"
    assert fake.intents[live].payment_method == "pm_1"
    assert fake.intents[two].status == "requires_confirmation"
    assert fake.cancelled == [(one, "abandoned")]
    assert len(fake.intents) == 3


@pytest.mark.parametrize("cancel_keeps_card", [True, False])
def test_two_card_tabs_can_both_still_pay(db, invoice, cancel_keeps_card):
    """2026-10-12 audit of this change. Two tabs on one invoice, each past
    step 1 on its own card, pressing Pay in turn. The page charges only when
    the re-POST hands back the intent it is showing; otherwise it re-shows
    the new one ("The amount changed"). Sweeping on the Pay step made each
    tab cancel the other's intent on every click, so neither ever charged.
    Run both ways on whether Stripe still lists the card on a cancelled
    intent: the docs do not say, and the answer must not matter."""
    with _stripe() as fake:

        def cancel_dropping_card(pid, **kw):
            out = fake.cancel(pid, **kw)
            fake.intents[pid].payment_method = None
            return out

        dropping = patch("stripe.PaymentIntent.cancel", side_effect=cancel_dropping_card)
        with contextlib.nullcontext() if cancel_keeps_card else dropping:
            shown = {
                "a": _press(db, invoice, method="card", payment_method_id="pm_a")["payment_intent_id"],
                "b": _press(db, invoice, method="card", payment_method_id="pm_b")["payment_intent_id"],
            }
            charged = {"a": False, "b": False}
            for _ in range(3):
                for tab in ("a", "b"):
                    got = _press(db, invoice, method="card", payment_method_id=f"pm_{tab}")["payment_intent_id"]
                    charged[tab] = charged[tab] or got == shown[tab]
                    shown[tab] = got

    assert charged == {"a": True, "b": True}
    assert all(fake.intents[i].status == "requires_confirmation" for i in shown.values())


def test_a_declined_card_retried_does_not_cancel_the_other_tab(db, invoice):
    """2026-10-12 audit, round 2. A decline sends the intent back to
    `requires_payment_method` and Stripe likely clears its `payment_method`.
    Retrying the same card replays its key, so it is not a card switch and
    must leave the other tab's intent alone."""
    with _stripe() as fake:
        a = _press(db, invoice, method="card", payment_method_id="pm_a")["payment_intent_id"]
        b = _press(db, invoice, method="card", payment_method_id="pm_b")["payment_intent_id"]
        a2 = _press(db, invoice, method="card", payment_method_id="pm_a")["payment_intent_id"]
        fake.intents[a2].status = "requires_payment_method"  # declined
        fake.intents[a2].payment_method = None
        before = list(fake.cancelled)
        b_again = _press(db, invoice, method="card", payment_method_id="pm_b")["payment_intent_id"]
        _press(db, invoice, method="card", payment_method_id="pm_a")

    assert before == [(a, "abandoned")], "b's step 1 cancels a's step-1 intent, nothing else"
    assert fake.cancelled == before
    assert b_again == b and fake.intents[b].status == "requires_confirmation"


def test_bank_and_card_sweeps_do_not_cross(db, invoice):
    """A card switch leaves an open bank intent to the bank sweep, and the
    bank sweep (every press) never touches a card intent."""
    with _stripe() as fake:
        bank = _press(db, invoice)["payment_intent_id"]
        fake.link_account(bank, "pm_bank_1")
        card = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]

    # The pm_1 press runs the bank sweep, as every press does, and cancels
    # the account; the card it minted is untouched by it.
    assert fake.cancelled == [(bank, "abandoned")]
    assert fake.intents[card].status == "requires_confirmation"


def test_an_intent_in_a_3ds_check_is_not_swept(db, invoice):
    """CONTROL. `requires_action` is a customer authenticating card pm_1 in
    another tab right now; a pm_2 press must not cancel it under them."""
    with _stripe() as fake:
        one = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]
        fake.intents[one].status = "requires_action"
        _press(db, invoice, method="card", payment_method_id="pm_2")

    assert fake.intents[one].status == "requires_action"
    assert fake.cancelled == []


def test_an_intent_that_entered_3ds_after_the_list_is_not_swept(db, invoice):
    """2026-10-12 audit. The register is read at the start of the request;
    pm_1's tab can start its 3D Secure check before the sweep reaches it.
    The intent is read again before the cancel."""
    with _stripe() as fake:
        one = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]
        listed = fake.list

        def list_then_authenticate(**kw):
            page = listed(**kw)
            fake.intents[one].status = "requires_action"
            return page

        with patch("stripe.PaymentIntent.list", side_effect=list_then_authenticate):
            _press(db, invoice, method="card", payment_method_id="pm_2")

    assert fake.intents[one].status == "requires_action"
    assert fake.cancelled == []


def test_a_press_without_a_card_attached_sweeps_no_card(db, invoice):
    """CONTROL. An ACH press or a one-step card press attaches no card, so
    it has no card to keep and cancels none."""
    with _stripe() as fake:
        one = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]
        _press(db, invoice, method="ach")
        _press(db, invoice, method="card")

    assert fake.intents[one].status == "requires_confirmation"
    assert fake.cancelled == []


def test_a_live_replay_mints_nothing(db, invoice):
    """CONTROL. The happy path is unchanged: same intent, one create each."""
    with _stripe() as fake:
        a = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]
        b = _press(db, invoice, method="card", payment_method_id="pm_1")["payment_intent_id"]

    assert a == b
    assert len(fake.keys) == 2 and len(set(fake.keys)) == 1
    assert fake.cancelled == []
