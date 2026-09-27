"""A card that settled at Stripe cannot be collected a second time (GDXA-84).

The class M16 closed for bank transfers, closed for cards: *money has already
moved on this invoice and our books do not know yet, so the pay page collects
again.*

A confirm that succeeds at Stripe whose response never reaches the browser — a
network blip, a backgrounded tab, a mobile handoff — puts the thrown
`confirmCardPayment` into the pay page's `catch`, so the Pay button comes back
enabled with the full balance still showing and `/confirm` is never called.
Nothing local changed, so until the webhook lands `create-intent` happily mints
a second chargeable intent for the FULL amount. The webhook's latency was the
window, and the balance reaching zero was its only closer.

The fix is keyed on the INVOICE, not on Stripe's idempotency replay: the
surcharge flow folds the PaymentMethod id into the key, so "Use a different
card" mints under a brand-new key and no replay exists at all. Any `succeeded`
intent carrying this invoice's `metadata.invoice_id` is money that moved.

And it RECORDS rather than refuses — a bare 409 parks the customer on a dead
page and leaves the office an invoice that looks unpaid.

What these lock:

  - a settled-but-unrecorded intent is booked, and no second intent is minted;
  - the amount is re-resolved, so a PARTIAL recovery still lets the customer
    pay what is left (a blanket refusal would wedge a payable invoice);
  - the predicate for "already in our books" is the one `_mark_invoice_paid`
    uses, so a recovery followed by the webhook records exactly ONE payment;
  - the rail is read, not guessed, and an unreadable rail skips the recovery
    rather than booking a bank debit as a card payment;
  - it fails OPEN — a Stripe outage, or a recorder failure, must not refuse a
    legitimate payer;
  - `_ach_in_flight` is NOT widened to `succeeded` (a recorded settled intent
    must keep passing, or a partial payment wedges the balance);
  - the office gets a trail row, because nobody entered this payment;
  - the 409 sentence the pay page shows survives the app's HTTPException
    handler — that handler rebuilds the response from `exc.detail` alone and
    DROPS `exc.headers`, so the status and the string are the whole contract.

Deliberate failure directions asserted below: both fail-open legs, and the
unreadable-rail skip (which leaves the money for the webhook rather than
booking it on a guess).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import secrets
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from gdx_dispatch.core.audit import AuditLog, ensure_audit_table
from gdx_dispatch.core.payments import CreateIntentRequest, create_intent
from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceLine, Payment

COMPANY = "22222222-2222-2222-2222-222222222222"

# The sentence the pay page shows. Pinned as a literal on purpose: it is the
# only discriminator the browser gets (see the module docstring), so changing
# it in `core/payments.py` must turn this red and put the pay page back on
# somebody's list.
ALREADY_PAID_SENTENCE = (
    "Your payment already went through — we just hadn't finished recording it. "
    "This invoice is paid in full; you don't need to pay again."
)


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    # `_audit_money_event` creates this on demand, so the control tests — which
    # assert NO row was written — need it to exist before they can query it.
    ensure_audit_table(tenant_db)
    # The sweep is queued after every recorded payment and costs ~2s against an
    # unreachable broker. Its own behaviour is covered by
    # test_stale_intent_cancellation.py; one test below asserts the recovery
    # does participate in it.
    with patch("gdx_dispatch.tasks.stale_intent_sweep.enqueue_stale_intent_sweep"):
        yield tenant_db


@pytest.fixture
def invoice(db):
    return _invoice(db, total="200.00")


def _invoice(db, total="200.00"):
    cust = Customer(id=uuid4(), name="Pay Page Customer", company_id=COMPANY)
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
        invoice_date=dt.date(2026, 9, 26),
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(
        invoice_id=inv.id, description="Torsion spring", quantity=1,
        unit_price=Decimal(total), line_total=Decimal(total), company_id=COMPANY,
    ))
    db.commit()
    db.refresh(inv)
    return inv


def _pi(pid, status, invoice_id, *, amount=20000, received=None, types=("card",),
        payment_method=None, surcharge_cents=None):
    """One PaymentIntent as `PaymentIntent.list` hands it over."""
    meta = {"invoice_id": str(invoice_id)}
    if surcharge_cents is not None:
        meta["surcharge_cents"] = str(surcharge_cents)
    return SimpleNamespace(
        id=pid, status=status, amount=amount,
        amount_received=amount if received is None else received,
        payment_method_types=list(types), payment_method=payment_method,
        metadata=meta, next_action=None,
    )


# What a fresh mint hands back. `_create_usable_intent` creates, then retrieves
# to prove liveness, and returns the retrieved object.
_FRESH = SimpleNamespace(
    id="pi_fresh", status="requires_payment_method", client_secret="cs_fresh",
    payment_method=None, metadata={}, amount=0, amount_details=None,
)


@contextlib.contextmanager
def _stripe(rows, *, list_raises=None, retrieve=None):
    """Patch every Stripe call this path can make.

    `list` serves both the ACH in-flight probe and the recovery scan; `create`
    plus `retrieve` serve the mint's liveness check; `retrieve` also serves the
    surcharge split and the rail read.
    """
    def _list(**_kwargs):
        if list_raises is not None:
            raise list_raises
        return SimpleNamespace(data=list(rows), has_more=False)

    def _retrieve(pid, **_kwargs):
        return (retrieve or {}).get(pid, _FRESH)

    with patch("stripe.PaymentIntent.list", side_effect=_list) as lst, \
            patch("stripe.PaymentIntent.create", return_value=_FRESH) as create, \
            patch("stripe.PaymentIntent.retrieve", side_effect=_retrieve) as retr:
        yield SimpleNamespace(list=lst, create=create, retrieve=retr)


def _mint(db, invoice, *, method="card", payment_method_id=None):
    """Drive the real `create-intent` handler for this invoice."""
    return create_intent(
        CreateIntentRequest(
            invoice_token=invoice.public_token, method=method,
            payment_method_id=payment_method_id,
        ),
        SimpleNamespace(state=SimpleNamespace(tenant={})),
        db=db,
    )


def _payments(db, invoice):
    return db.execute(
        select(Payment).where(Payment.invoice_id == invoice.id)
    ).scalars().all()


def _balance(db, invoice):
    db.refresh(invoice)
    return float(invoice.balance_due or 0)


# ── the defect ─────────────────────────────────────────────────────────────


def test_a_settled_card_is_recorded_instead_of_charged_again(db, invoice):
    """The window itself. Stripe holds a succeeded $200 card intent, our books
    hold nothing, and the customer clicks Pay again."""
    with _stripe([_pi("pi_settled", "succeeded", invoice.id)]) as api, \
            pytest.raises(HTTPException) as exc:
        _mint(db, invoice)

    # A call here is the defect itself: a second chargeable intent for money
    # that has already been collected.
    api.create.assert_not_called()
    assert exc.value.status_code == 409
    assert "already went through" in exc.value.detail

    rows = _payments(db, invoice)
    assert len(rows) == 1, "the settled intent was not recorded"
    assert rows[0].reference == "pi_settled"
    assert float(rows[0].amount) == 200.00
    assert rows[0].method == "card"
    assert _balance(db, invoice) == 0.0


def test_an_unsettled_intent_still_gets_a_fresh_one(db, invoice):
    """CONTROL for the test above — identical harness, the only variable is the
    live status. An abandoned page is not money that moved, so the customer
    must still be able to pay."""
    with _stripe([_pi("pi_abandoned", "requires_payment_method", invoice.id)]) as api:
        out = _mint(db, invoice)

    api.create.assert_called_once()
    assert out["client_secret"] == "cs_fresh"
    assert out["amount"] == 20000
    assert _payments(db, invoice) == []
    assert _balance(db, invoice) == 200.00


def test_a_partial_recovery_still_lets_the_customer_pay_the_rest(db, invoice):
    """A settled $50 intent on a $200 invoice is recorded, and the mint goes
    ahead for the $150 that is actually still owed. A blanket refusal here
    would wedge a payable invoice — which is the whole reason this is a
    recorder and not a 409."""
    with _stripe([_pi("pi_part", "succeeded", invoice.id, amount=5000)]) as api:
        out = _mint(db, invoice)

    rows = _payments(db, invoice)
    assert len(rows) == 1 and float(rows[0].amount) == 50.00
    assert _balance(db, invoice) == 150.00
    api.create.assert_called_once()
    assert out["amount"] == 15000, "the mint must charge the RE-RESOLVED balance, not the old one"
    assert out["invoice_amount"] == 15000


def test_a_second_card_under_a_brand_new_key_is_still_caught(db, invoice):
    """The leg a replay-site fix cannot reach. "Use a different card" nulls the
    PaymentMethod id, so the next submit mints under a key Stripe has never
    seen — no replay, nothing for `_create_usable_intent` to notice. Keyed on
    the invoice, it is caught anyway."""
    with _stripe([_pi("pi_settled", "succeeded", invoice.id)]) as api, \
            pytest.raises(HTTPException) as exc:
        _mint(db, invoice, payment_method_id="pm_a_different_card")

    api.create.assert_not_called()
    assert exc.value.status_code == 409
    assert len(_payments(db, invoice)) == 1


def test_the_bank_rail_mint_is_gated_too(db, invoice):
    """The customer whose card confirm was lost may switch tabs and try a bank
    transfer. Same money, same invoice, same answer."""
    with _stripe([_pi("pi_settled", "succeeded", invoice.id)]) as api, \
            pytest.raises(HTTPException) as exc:
        _mint(db, invoice, method="ach")

    api.create.assert_not_called()
    assert exc.value.status_code == 409
    assert len(_payments(db, invoice)) == 1


# ── idempotency: the webhook still lands afterwards ────────────────────────


def test_an_already_recorded_intent_is_not_recorded_twice(db, invoice):
    """CONTROL for the recovery predicate. The webhook won the race, so the
    money is already in the books — the recovery must find nothing to do, and
    must not block the customer paying the rest."""
    from gdx_dispatch.core.payments import _mark_invoice_paid

    _mark_invoice_paid(
        invoice, db, external_ref="pi_settled", method="card",
        amount=50.0, source="stripe-webhook",
    )
    assert _balance(db, invoice) == 150.00

    with _stripe([_pi("pi_settled", "succeeded", invoice.id, amount=5000)]) as api:
        out = _mint(db, invoice)

    assert len(_payments(db, invoice)) == 1, "the recovery double-recorded a payment"
    api.create.assert_called_once()
    assert out["amount"] == 15000


def test_the_webhook_after_a_recovery_records_exactly_one_payment(db, invoice):
    """The direction that actually happens in production: the recovery books
    it, then the delayed webhook arrives for the same intent."""
    from gdx_dispatch.core.payments import handle_payment_webhook

    with _stripe([_pi("pi_settled", "succeeded", invoice.id)]), pytest.raises(HTTPException):
        _mint(db, invoice)
    assert len(_payments(db, invoice)) == 1

    handle_payment_webhook(
        {
            "type": "payment_intent.succeeded",
            "data": {"object": {
                "id": "pi_settled", "amount": 20000, "amount_received": 20000,
                "payment_method_types": ["card"],
                "metadata": {"invoice_id": str(invoice.id)},
            }},
        },
        db,
    )
    rows = _payments(db, invoice)
    assert len(rows) == 1, "the webhook booked a second payment for one charge"
    assert _balance(db, invoice) == 0.0


# ── money that was deliberately reversed must stay reversed ────────────────
#
# A Stripe PaymentIntent stays `succeeded` forever. A refund, a dispute, or an
# office void does not change it — it sets `voided_at` on OUR row and re-opens
# the balance. So "succeeded at Stripe, no live payment row" describes both
# "we failed to book it" AND "we booked it and un-booked it on purpose", and
# only the second is none of the recovery's business.
#
# This is why the recovery cannot reuse `_mark_invoice_paid`'s idempotency
# predicate: M14 makes that one EXCLUDE voided rows on purpose, so a redelivered
# webhook can re-record a wrongly-reversed payment. The recovery needs the
# opposite question — "have the books ever seen this reference at all".


def test_a_refunded_card_is_not_re_recorded(db, invoice):
    """The defect the adversarial audit found in the first version of this fix.

    Stripe refunds the charge, the webhook voids our row, the balance comes
    back. The intent is still `succeeded`. Re-recording it would silently undo
    the refund, flip the invoice to paid, and tell the customer they had
    already paid — money the office had given back."""
    from gdx_dispatch.core.payments import _mark_invoice_paid, _reverse_recorded_payment

    _mark_invoice_paid(
        invoice, db, external_ref="pi_refunded", method="card",
        amount=200.0, source="stripe-webhook",
    )
    assert _balance(db, invoice) == 0.0
    out = _reverse_recorded_payment(db, "pi_refunded", "charge.refunded")
    assert out["status"] == "reversed"
    assert _balance(db, invoice) == 200.00, "the refund must re-open the balance"

    with _stripe([_pi("pi_refunded", "succeeded", invoice.id)]) as api:
        got = _mint(db, invoice)

    live = [p for p in _payments(db, invoice) if p.voided_at is None]
    assert live == [], "a refunded payment was re-recorded — the refund is undone"
    assert _balance(db, invoice) == 200.00
    # And the customer is not blocked: they legitimately owe this again.
    api.create.assert_called_once()
    assert got["amount"] == 20000


def test_an_office_voided_payment_is_not_re_recorded(db, invoice):
    """Same shape through the office rather than Stripe: staff void a mis-keyed
    card payment. The intent is untouched at Stripe, so the recovery would
    resurrect exactly what a human decided to reverse."""
    from gdx_dispatch.core.payments import _mark_invoice_paid, apply_payment_void
    from gdx_dispatch.routers.invoices import _recalculate_invoice, transition_invoice_status

    _mark_invoice_paid(
        invoice, db, external_ref="pi_miskeyed", method="card",
        amount=200.0, source="stripe-webhook",
    )
    row = _payments(db, invoice)[0]
    # Exactly what the office void route does (routers/invoices.py:3452-3463):
    # `apply_payment_void` deliberately leaves the INVOICE to its caller, so a
    # test that stops at the void is testing a state the app never reaches.
    apply_payment_void(db, row, reason="office_void", actor="office-user")
    _recalculate_invoice(invoice, db)
    if invoice.status == "paid" and float(invoice.balance_due or 0) > 0:
        transition_invoice_status(db, invoice, "sent", actor="office-user")
        invoice.paid_at = None
    db.commit()
    assert _balance(db, invoice) == 200.00

    with _stripe([_pi("pi_miskeyed", "succeeded", invoice.id)]) as api:
        _mint(db, invoice)

    assert [p for p in _payments(db, invoice) if p.voided_at is None] == []
    api.create.assert_called_once()


def test_an_intent_the_books_have_never_seen_is_still_recovered(db, invoice):
    """CONTROL for the two above. The skip must be caused by the reversal
    history, not by the recovery having been switched off."""
    with _stripe([_pi("pi_never_seen", "succeeded", invoice.id)]), \
            pytest.raises(HTTPException):
        _mint(db, invoice)

    live = [p for p in _payments(db, invoice) if p.voided_at is None]
    assert len(live) == 1 and live[0].reference == "pi_never_seen"


# ── binding and rail ───────────────────────────────────────────────────────


def test_another_invoices_settled_intent_is_ignored(db, invoice):
    """`metadata.invoice_id` is the binding. Someone else's payment must never
    settle this bill — nor stop this customer paying it."""
    with _stripe([_pi("pi_theirs", "succeeded", uuid4())]) as api:
        out = _mint(db, invoice)

    api.create.assert_called_once()
    assert _payments(db, invoice) == []
    assert out["amount"] == 20000


@pytest.mark.parametrize(
    ("types", "expected"),
    [(("card",), "card"), (("us_bank_account",), "ach")],
)
def test_a_recovered_payment_books_on_the_rail_that_paid(db, invoice, types, expected):
    """A hard-coded "card" here would book a settled bank debit as a card
    payment. Both directions asserted, so neither reading is free."""
    with _stripe([_pi("pi_settled", "succeeded", invoice.id, types=types)]), \
            pytest.raises(HTTPException):
        _mint(db, invoice)

    rows = _payments(db, invoice)
    assert len(rows) == 1
    assert rows[0].method == expected


def test_an_unreadable_rail_is_left_for_the_webhook(db, invoice):
    """The allowed list names both rails and the PaymentMethod cannot be read.
    Recording it would be a coin-flip between AR and the bank rail, so it is
    NOT recorded — and the customer is not blocked either. The webhook, which
    has a retrier behind it, remains the fallback."""
    def _pm_down(_pm, **_kwargs):
        raise RuntimeError("stripe down")

    rows = [_pi(
        "pi_settled", "succeeded", invoice.id,
        types=("card", "us_bank_account"), payment_method="pm_unreadable",
    )]
    with _stripe(rows) as api, patch("stripe.PaymentMethod.retrieve", side_effect=_pm_down):
        out = _mint(db, invoice)

    assert _payments(db, invoice) == [], "money was booked on a guessed rail"
    api.create.assert_called_once()
    assert out["amount"] == 20000


def test_an_ambiguous_list_with_a_readable_card_is_recorded(db, invoice):
    """CONTROL for the skip above — same ambiguous list, PaymentMethod
    readable. The skip must be caused by the unreadable rail and nothing else."""
    rows = [_pi(
        "pi_settled", "succeeded", invoice.id,
        types=("card", "us_bank_account"), payment_method="pm_readable",
    )]
    with _stripe(rows), \
            patch("stripe.PaymentMethod.retrieve", return_value=SimpleNamespace(type="card")), \
            pytest.raises(HTTPException):
        _mint(db, invoice)

    got = _payments(db, invoice)
    assert len(got) == 1 and got[0].method == "card"


# ── the surcharge leg ──────────────────────────────────────────────────────


def test_a_recovered_surcharged_card_splits_the_fee_off_the_receipt(db, invoice):
    """A surcharged intent is minted for balance + fee. The fee settles no AR:
    the invoice gets $200 and the Payment row carries the $5.80 separately, the
    same split `/confirm` and the webhook already do."""
    settled = _pi(
        "pi_settled", "succeeded", invoice.id, amount=20580, surcharge_cents=580,
    )
    live = SimpleNamespace(
        id="pi_settled",
        amount_details=SimpleNamespace(surcharge=SimpleNamespace(amount=580)),
    )
    with _stripe([settled], retrieve={"pi_settled": live}) as api, \
            pytest.raises(HTTPException):
        _mint(db, invoice)

    api.create.assert_not_called()
    rows = _payments(db, invoice)
    assert len(rows) == 1
    assert float(rows[0].amount) == 200.00, "the card fee must not settle AR"
    assert float(rows[0].surcharge_amount) == 5.80
    assert _balance(db, invoice) == 0.0


def test_a_settled_intent_reporting_no_money_is_not_booked(db, invoice):
    """CONTROL on the amount. `_mark_invoice_paid` reads a zero amount as
    "legacy event — record the whole remaining balance", which on a succeeded
    intent reporting nothing received would invent a $200 payment."""
    with _stripe([_pi("pi_zero", "succeeded", invoice.id, amount=0, received=0)]) as api:
        out = _mint(db, invoice)

    assert _payments(db, invoice) == [], "a payment was invented from a zero receipt"
    api.create.assert_called_once()
    assert out["amount"] == 20000


# ── failure directions: fail OPEN ──────────────────────────────────────────


def test_a_stripe_outage_still_lets_the_customer_pay(db, invoice):
    """Fail OPEN, like the mint gates beside it. Refusing to take money because
    Stripe could not be read would strand a legitimate payer for as long as the
    outage lasts; the M12 sweep and `payment_exceeds_receivable` cover it."""
    with _stripe([], list_raises=RuntimeError("stripe down")) as api:
        out = _mint(db, invoice)

    api.create.assert_called_once()
    assert out["client_secret"] == "cs_fresh"


def test_a_recorder_failure_does_not_refuse_the_payer(db, invoice):
    """A database hiccup recording the recovery must not turn the pay page into
    a dead end, and must not leave the session poisoned for the mint that
    follows it."""
    with _stripe([_pi("pi_settled", "succeeded", invoice.id)]) as api, \
            patch(
                "gdx_dispatch.core.payments._mark_invoice_paid",
                side_effect=RuntimeError("db hiccup"),
            ):
        out = _mint(db, invoice)

    api.create.assert_called_once()
    assert out["amount"] == 20000
    assert _payments(db, invoice) == []


def test_a_vanished_balance_refuses_rather_than_asking_stripe_for_zero(db, invoice):
    """The recorder commits before it returns, so money can land on a path that
    reports nothing back — its own post-commit raise, or a webhook committing
    concurrently. The refusal is therefore keyed on the AMOUNT, not on what the
    recovery returned, so the mint can never be handed a zero (Stripe rejects
    that with a 402, which reads to the customer as a card problem).

    Paired with `test_an_unsettled_intent_still_gets_a_fresh_one`, where the
    same call with a real balance mints normally."""
    def _zero_the_balance(inv, **_kwargs):
        inv.balance_due = Decimal("0.00")
        db.commit()
        return []

    with _stripe([]) as api, patch(
        "gdx_dispatch.core.payments._settle_unrecorded_intents",
        side_effect=_zero_the_balance,
    ), pytest.raises(HTTPException) as exc:
        _mint(db, invoice)

    api.create.assert_not_called()
    assert exc.value.status_code == 409
    # The pre-existing sentence, not the recovery one: nothing was recovered
    # here, so claiming "we found your payment" would be a guess.
    assert exc.value.detail == "This invoice has no balance due."


def test_a_clean_invoice_costs_no_extra_stripe_calls(db, invoice):
    """The scan is the ACH probe's scan. On the ordinary first visit it is one
    `list` — the recovery must not add a second round trip to every payment."""
    with _stripe([]) as api:
        _mint(db, invoice)
    assert api.list.call_count == 1


# ── `_ach_in_flight` must NOT learn about `succeeded` ──────────────────────


def test_a_settled_intent_is_still_not_a_payment_in_flight(invoice):
    """Recorded-vs-unrecorded is why the recovery needs the session, and why
    this probe must stay stateless. Widening it to `succeeded` would lock a
    payable invoice the moment a partial payment settled
    (test_ach_processing_window.py::test_only_processing_counts)."""
    from gdx_dispatch.core.payments import _ach_in_flight

    with _stripe([_pi("pi_settled", "succeeded", invoice.id)]):
        assert _ach_in_flight(invoice) is None


# ── the trail ──────────────────────────────────────────────────────────────


def test_a_recovery_lands_on_the_invoices_trail(db, invoice):
    """Nobody entered this payment. Without the row the office finds a payment
    on the invoice with nothing to explain where it came from."""
    with _stripe([_pi("pi_settled", "succeeded", invoice.id)]), pytest.raises(HTTPException):
        _mint(db, invoice)

    row = db.query(AuditLog).filter(
        AuditLog.action == "payment_recovered_from_stripe"
    ).one()
    assert str(row.entity_id) == str(invoice.id)
    assert row.user_id == "stripe-recovered", "a money event may not be filed anonymously"
    assert row.details["intent_id"] == "pi_settled"
    assert row.details["op"] == "create-intent:card"
    assert row.details["method"] == "card"
    assert row.details["amount"] == 200.0
    assert row.details["payment_id"]


def test_no_recovery_writes_no_trail_row(db, invoice):
    """CONTROL — the row must be caused by a recovery, not by every mint."""
    with _stripe([]):
        _mint(db, invoice)
    assert db.query(AuditLog).filter(
        AuditLog.action == "payment_recovered_from_stripe"
    ).count() == 0


def test_a_recovery_queues_the_stale_intent_sweep(db, invoice):
    """Money just landed, so any OTHER intent still open on this invoice can
    collect again — including the `-r<uuid>` re-mints this window produces."""
    with patch(
        "gdx_dispatch.tasks.stale_intent_sweep.enqueue_stale_intent_sweep"
    ) as sweep, _stripe([_pi("pi_settled", "succeeded", invoice.id)]), \
            pytest.raises(HTTPException):
        _mint(db, invoice)

    sweep.assert_called_once()
    assert "pi_settled" in sweep.call_args.kwargs["why"]


# ── the pay page's contract ────────────────────────────────────────────────


def test_the_pay_page_is_told_it_already_paid(db, invoice):
    """Through the real HTTP surface, with the app's own HTTPException handler
    registered (app.py:1236). That handler rebuilds the response from
    `exc.detail` alone and drops `exc.headers`, so this status and this exact
    sentence are the only discriminator the browser gets."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.core.error_handler import global_exception_handler
    from gdx_dispatch.core.payments import router

    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(HTTPException, global_exception_handler)
    app.dependency_overrides[get_db] = lambda: db

    with _stripe([_pi("pi_settled", "succeeded", invoice.id)]) as api, TestClient(app) as client:
        r = client.post(
            "/api/payments/create-intent",
            json={"invoice_token": invoice.public_token, "method": "card"},
        )

    api.create.assert_not_called()
    assert r.status_code == 409
    assert r.json()["detail"] == ALREADY_PAID_SENTENCE
    assert len(_payments(db, invoice)) == 1
