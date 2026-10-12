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
from sqlalchemy import text

from gdx_dispatch.core.audit import AuditLog, ensure_audit_table
from gdx_dispatch.core.payments import CreateIntentRequest, _create_usable_intent, create_intent
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


def test_switching_cards_cancels_the_first_cards_intent(db, invoice):
    """GDXA-790. "Use a different card" no longer leaves card A attached to
    an open intent; GDXA-784's converging re-mint is what makes this safe."""
    with _stripe() as fake:
        a = _press(db, invoice, method="card", payment_method_id="pm_card_a")
        b = _press(db, invoice, method="card", payment_method_id="pm_card_b")

    assert b != a
    assert fake.cancelled == [(a, "abandoned")]
    assert fake.intents[b].status == "requires_confirmation"


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
        # Only a cancel Stripe accepted is recorded: I0 yes, the refused I1 not yet.
        assert [r.details["intent_id"] for r in _cancel_rows(db)] == [i0]

        assert i2 not in (i0, i1)
        assert fake.intents[i2].status == "requires_payment_method"
        assert fake.intents[i1].status == "requires_confirmation"

        # Still in the register, so the next press tries it again.
        fake.fail_cancel.clear()
        _press(db, invoice)

    assert fake.intents[i1].status == "canceled"


# ── GDXA-787: a pay-page cancel leaves a row on the invoice trail ───────────


def _cancel_rows(db):
    """Read back what is COMMITTED: the rollback discards anything merely staged."""
    db.rollback()
    return (
        db.query(AuditLog)
        .filter(AuditLog.action == "abandoned_account_intent_canceled")
        .order_by(AuditLog.created_at)
        .all()
    )


def test_the_remint_sweep_records_each_cancel_on_the_invoice(db, invoice, caplog):
    """Invariant #1. Before GDXA-787 these cancels logged a WARNING and nothing
    else, so "my bank link vanished" had no trail to read."""
    with _stripe() as fake, caplog.at_level("WARNING"):
        i0 = _press(db, invoice)
        fake.link_account(i0, "pm_bank_1")
        i1 = _press(db, invoice)
        fake.link_account(i1, "pm_bank_2")
        _press(db, invoice)

    # `audit_best_effort` commits: nothing of create_intent's may be staged then.
    assert "audit_best_effort_caller_has_pending_work" not in caplog.text
    rows = _cancel_rows(db)
    assert [r.details["intent_id"] for r in rows] == [i0, i1]
    for row in rows:
        assert row.user_id == "stripe-pay-page", "a money-object change with no named actor"
        assert row.entity_type == "invoice"
        assert str(row.entity_id) == str(invoice.id)
        assert row.details["why"] == "remint-sweep"
        assert row.details["status"] == "canceled"


def test_no_cancel_no_row(db, invoice):
    """CONTROL. A reload that cancels nothing writes nothing."""
    with _stripe() as fake:
        _press(db, invoice)
        _press(db, invoice)
    assert fake.cancelled == []
    assert _cancel_rows(db) == []


def test_the_replay_cancel_records_its_own_row(db, invoice):
    """The other cancel site: the key replays an intent that still holds an
    account the remint sweep did not see (here, `_create_usable_intent` called
    on its own, as when the register read came back short)."""
    kw = dict(
        amount=20000, currency="usd", metadata={"invoice_id": str(invoice.id)},
        payment_method_types=["us_bank_account"], idempotency_key="gdx-pi-replay-ach",
    )
    with _stripe() as fake:
        first = _create_usable_intent(db=db, **kw)
        fake.link_account(first.id, "pm_bank_1")
        fresh = _create_usable_intent(db=db, **kw)

    assert fresh.id != first.id
    assert fake.cancelled == [(first.id, "abandoned")]
    rows = _cancel_rows(db)
    assert len(rows) == 1
    assert rows[0].user_id == "stripe-pay-page"
    assert str(rows[0].entity_id) == str(invoice.id)
    assert rows[0].details == {"intent_id": first.id, "why": "replay", "status": "canceled"}


def test_a_cancel_on_a_re_mint_hop_records_its_own_row(db, invoice):
    """GDXA-784's chain. The first replay is dead (no cancel, no row); its
    `{key}-r{dead}` hop replays an intent that still holds an account, and
    that cancel is recorded like the first-replay one."""
    kw = dict(
        amount=20000, currency="usd", metadata={"invoice_id": str(invoice.id)},
        payment_method_types=["us_bank_account"], idempotency_key="gdx-pi-hop-ach",
    )
    with _stripe() as fake:
        i0 = _create_usable_intent(db=db, **kw)
        fake.intents[i0.id].status = "canceled"  # the settle-time sweep
        i1 = _create_usable_intent(db=db, **kw)
        fake.link_account(i1.id, "pm_bank_1")
        i2 = _create_usable_intent(db=db, **kw)

    assert len({i0.id, i1.id, i2.id}) == 3
    assert fake.cancelled == [(i1.id, "abandoned")]
    rows = _cancel_rows(db)
    assert len(rows) == 1
    assert rows[0].user_id == "stripe-pay-page"
    assert str(rows[0].entity_id) == str(invoice.id)
    assert rows[0].details == {"intent_id": i1.id, "why": "replay", "status": "canceled"}


def test_a_refused_audit_row_does_not_stop_the_cancel_or_the_mint(db, invoice, caplog):
    """Best-effort: losing the trail row is survivable, losing the customer's
    checkout is not. The storage layer refuses every audit INSERT; both
    cancels still happen and the customer still gets a live intent."""
    ensure_audit_table(db)
    db.execute(text(
        "CREATE TRIGGER audit_logs_refuse_insert BEFORE INSERT ON audit_logs "
        "BEGIN SELECT RAISE(ABORT, 'audit storage refuses this row'); END;"
    ))
    db.commit()
    kw = dict(
        amount=20000, currency="usd", metadata={"invoice_id": str(invoice.id)},
        payment_method_types=["us_bank_account"], idempotency_key="gdx-pi-refused-ach",
    )
    with _stripe() as fake, caplog.at_level("ERROR"):
        i0 = _press(db, invoice)
        fake.link_account(i0, "pm_bank_1")
        i1 = _press(db, invoice)
        first = _create_usable_intent(db=db, **kw)
        fake.link_account(first.id, "pm_bank_2")
        fresh = _create_usable_intent(db=db, **kw)

    assert fake.cancelled == [(i0, "abandoned"), (first.id, "abandoned")]
    assert fake.intents[i1].status == "requires_payment_method"
    assert fake.intents[fresh.id].status == "requires_payment_method"
    assert caplog.text.count("abandoned_account_intent_canceled_audit_failed") == 2
    assert _cancel_rows(db) == []
