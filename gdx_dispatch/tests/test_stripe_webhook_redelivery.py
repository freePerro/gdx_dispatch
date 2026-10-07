"""A redelivered Stripe event changes nothing (GDXA-357, from GDXA-349).

Stripe states the hazard in so many words (https://docs.stripe.com/webhooks,
read 2026-10-06, page on API version 2026-09-30.preview):

* "Webhook endpoints might occasionally receive the same event more than once.
  You can guard against duplicated event receipts by logging the event IDs
  you've processed, and then not processing already-logged events."
* "Stripe doesn't guarantee the delivery of events in the order that they're
  generated ... Track event IDs to identify duplicate deliveries instead."
* Automatic retries run "for up to three days with an exponential back off",
  and a manual resend "doesn't cancel Stripe's automatic retry behavior, even
  if it results in a 2xx status code."

So the redelivery is not always the NEXT thing to arrive. A delivery that we
committed but Stripe recorded as timed out comes back hours later, after other
events for the same charge have landed.

What the event-id record does NOT cover, stated so nothing here implies it:
distinct events arriving out of order. A refund whose FIRST delivery lands
before the success's first delivery (because the success 500'd and is being
retried) is a different event id, and is outside this record by construction.

Every event goes through the real ``POST /stripe/webhook`` route with a real
``Stripe-Signature`` header, into the real handler, on an ORM-built schema
with GL posting ON. The assertions read rows back (Payment, GL entries, the
trial balance, the invoice), never mock call arguments.

Two shapes are pinned:

1. **A, A**: every money-moving event type delivered twice in a row. The
   handlers are idempotent per branch (``_recorded_payment`` on the success,
   "no live row to void" on every reversal, "no voided row" on the
   reinstatement), and these tests are the proof by execution the triage
   asked for.
2. **A, B, A**: a late redelivery of an event that was already handled,
   after a later event for the same charge. Per-branch idempotency cannot
   catch this, because the state that made the second A a no-op has been
   changed by B. The event-id record in ``stripe_webhook_events`` does, for
   every type but ``payment_intent.succeeded``: that one is never recorded,
   because M14 recovers through its re-send, and its replay after a refund or
   dispute is refused in the handler (``_deliberately_voided``).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import secrets
import time
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Payment
from gdx_dispatch.modules.ledger.models import GlJournalEntry
from gdx_dispatch.modules.ledger.service import ensure_gl_seed, transition_invoice_status

COMPANY = "11111111-1111-1111-1111-111111111111"
SECRET = "whsec_test_redelivery_" + "x" * 24
INTENT = "pi_redelivery_1"
CHARGE = "ch_redelivery_1"
CENTS = 50_000


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    # No broker in the test image: the sweep would cost seconds per payment.
    # It runs AFTER the commit and never decides what is recorded.
    monkeypatch.setattr(
        "gdx_dispatch.tasks.stale_intent_sweep.enqueue_stale_intent_sweep",
        lambda *a, **k: False,
    )
    # The failure events ask Stripe whether the attempt is current. Answer
    # "yes, CHARGE is the intent's latest", so they take the reversing branch,
    # which is the one that moves money.
    pi = MagicMock()
    pi.id, pi.status, pi.latest_charge = INTENT, "requires_payment_method", CHARGE
    monkeypatch.setattr("stripe.PaymentIntent.retrieve", lambda *a, **k: pi)
    settings = ensure_gl_seed(tenant_db, COMPANY)
    settings.ledger_posting_enabled = True
    tenant_db.commit()
    return tenant_db


@pytest.fixture
def client(db):
    from gdx_dispatch.routers.stripe_webhook import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


@pytest.fixture
def invoice(db):
    inv = Invoice(
        id=uuid4(),
        customer_id=uuid4(),
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status="draft",
        subtotal=Decimal("500.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("500.00"),
        balance_due=Decimal("500.00"),
        invoice_date=dt.date(2026, 7, 1),
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=inv.id, description="Opener install", quantity=1,
            unit_price=Decimal("500.00"), line_total=Decimal("500.00"),
            company_id=COMPANY,
        )
    )
    db.commit()
    transition_invoice_status(db, inv, "sent")
    db.commit()
    db.refresh(inv)
    return inv


# ── the wire ────────────────────────────────────────────────────────────────


def _deliver(client, event: dict) -> dict:
    """POST ``event`` the way Stripe does: raw body plus a v1 HMAC signature.

    Stripe signs each delivery attempt afresh ("If Stripe retries an event ...
    we generate a new signature and timestamp"), so a redelivery is the same
    body under a new header, which is what this produces on every call.
    """
    body = json.dumps(event, separators=(",", ":"))
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    resp = client.post(
        "/stripe/webhook",
        content=body,
        headers={"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _event(etype: str, obj: dict, event_id: str | None = None) -> dict:
    return {
        "id": event_id or f"evt_{uuid4().hex[:24]}",
        "object": "event",
        "type": etype,
        "data": {"object": obj},
    }


def _succeeded(invoice, event_id=None):
    return _event(
        "payment_intent.succeeded",
        {
            "id": INTENT, "object": "payment_intent", "status": "succeeded",
            "amount": CENTS, "amount_received": CENTS, "currency": "usd",
            "payment_method_types": ["card"], "latest_charge": CHARGE,
            "metadata": {"invoice_id": str(invoice.id)},
        },
        event_id,
    )


def _refunded(amount_refunded=CENTS, event_id=None):
    return _event(
        "charge.refunded",
        {
            "id": CHARGE, "object": "charge", "payment_intent": INTENT,
            "amount": CENTS, "amount_refunded": amount_refunded,
            "refunded": amount_refunded >= CENTS,
        },
        event_id,
    )


def _dispute(etype, status="needs_response", event_id=None):
    return _event(
        etype,
        {
            "id": "dp_redelivery_1", "object": "dispute", "charge": CHARGE,
            "payment_intent": INTENT, "amount": CENTS, "status": status,
        },
        event_id,
    )


def _charge_failed(event_id=None):
    return _event(
        "charge.failed",
        {"id": CHARGE, "object": "charge", "payment_intent": INTENT},
        event_id,
    )


def _intent_failed(invoice, event_id=None):
    return _event(
        "payment_intent.payment_failed",
        {
            "id": INTENT, "object": "payment_intent", "status": "requires_payment_method",
            "amount": CENTS, "latest_charge": CHARGE, "payment_method_types": ["card"],
            "metadata": {"invoice_id": str(invoice.id)},
            "last_payment_error": {"message": "declined"},
        },
        event_id,
    )


# ── what the books say ──────────────────────────────────────────────────────


def _books(db, invoice) -> dict:
    """Every fact a duplicate could change: rows, postings, balances, status."""
    from gdx_dispatch.modules.ledger.reports import trial_balance

    db.expire_all()
    inv = db.get(Invoice, invoice.id)
    rows = db.scalars(select(Payment).where(Payment.invoice_id == invoice.id)).all()
    tb = trial_balance(db, COMPANY, as_of=dt.date(2099, 12, 31))
    return {
        "payment_rows": len(rows),
        "live_payment_rows": sum(1 for p in rows if p.voided_at is None),
        "live_paid": round(sum(float(p.amount) for p in rows if p.voided_at is None), 2),
        "gl_entries": db.scalar(select(func.count()).select_from(GlJournalEntry)),
        "trial_balance": {
            r["code"]: r["debit_cents"] - r["credit_cents"]
            for r in tb["rows"]
            if r["debit_cents"] - r["credit_cents"]
        },
        "status": inv.status,
        "balance_due": float(inv.balance_due or 0),
    }


PAID = {"live_payment_rows": 1, "live_paid": 500.0, "status": "paid", "balance_due": 0.0}
OPEN = {"live_payment_rows": 0, "live_paid": 0.0, "status": "sent", "balance_due": 500.0}


def _assert_state(books: dict, expected: dict) -> None:
    got = {k: books[k] for k in expected}
    assert got == expected, books


def _pay(client, db, invoice) -> dict:
    """Record the card payment through the webhook itself, then check it took."""
    out = _deliver(client, _succeeded(invoice))
    assert out["result"]["status"] == "paid", out
    books = _books(db, invoice)
    _assert_state(books, PAID)
    assert books["trial_balance"] == {"1050": CENTS, "4000": -CENTS}, books
    return books


def _twice(client, db, invoice, event) -> tuple[dict, dict, dict]:
    """Deliver ``event`` twice, the same id both times. Books after each."""
    first = _deliver(client, event)
    after_first = _books(db, invoice)
    second = _deliver(client, event)
    after_second = _books(db, invoice)
    assert after_second == after_first, (
        f"a redelivered {event['type']} changed the books\n"
        f"first:  {first}\nsecond: {second}\n"
        f"after first:  {after_first}\nafter second: {after_second}"
    )
    return first, after_first, second


# ── A, A: every money-moving event type, delivered twice in a row ───────────


def test_succeeded_twice_records_one_payment_and_one_posting(client, db, invoice):
    before = _books(db, invoice)
    _first, books, _second = _twice(client, db, invoice, _succeeded(invoice))
    _assert_state(books, PAID)
    assert books["payment_rows"] == 1, books
    assert books["trial_balance"] == {"1050": CENTS, "4000": -CENTS}, books
    assert books["gl_entries"] > before["gl_entries"], "the first delivery posted nothing"


def test_full_refund_twice_voids_once(client, db, invoice):
    paid = _pay(client, db, invoice)
    _first, books, _second = _twice(client, db, invoice, _refunded())
    _assert_state(books, OPEN)
    assert books["payment_rows"] == 1, books
    assert books["trial_balance"].get("1050", 0) == 0, books
    assert books["gl_entries"] > paid["gl_entries"], "the void un-posted nothing"


def test_partial_refund_twice_leaves_the_payment_alone(client, db, invoice):
    paid = _pay(client, db, invoice)
    _first, books, _second = _twice(client, db, invoice, _refunded(amount_refunded=5_000))
    assert books == paid, books


@pytest.mark.parametrize("etype", ["charge.dispute.created", "charge.dispute.funds_withdrawn"])
def test_dispute_reversal_twice_reverses_once(client, db, invoice, etype):
    _pay(client, db, invoice)
    _first, books, _second = _twice(client, db, invoice, _dispute(etype))
    _assert_state(books, OPEN)
    assert books["payment_rows"] == 1, books
    assert books["trial_balance"].get("1050", 0) == 0, books


def test_dispute_inquiry_twice_moves_nothing(client, db, invoice):
    paid = _pay(client, db, invoice)
    _first, books, _second = _twice(
        client, db, invoice, _dispute("charge.dispute.created", status="warning_needs_response")
    )
    assert books == paid, books


def test_funds_reinstated_twice_reinstates_once(client, db, invoice):
    _pay(client, db, invoice)
    _deliver(client, _dispute("charge.dispute.funds_withdrawn"))
    _assert_state(_books(db, invoice), OPEN)
    _first, books, _second = _twice(
        client, db, invoice, _dispute("charge.dispute.funds_reinstated")
    )
    _assert_state(books, PAID)
    assert books["payment_rows"] == 1, "a reinstatement inserted a second row"
    assert books["trial_balance"] == {"1050": CENTS, "4000": -CENTS}, books


def test_charge_failed_twice_reverses_once(client, db, invoice):
    _pay(client, db, invoice)
    _first, books, _second = _twice(client, db, invoice, _charge_failed())
    _assert_state(books, OPEN)
    assert books["payment_rows"] == 1, books


def test_payment_failed_twice_reverses_once(client, db, invoice):
    _pay(client, db, invoice)
    _first, books, _second = _twice(client, db, invoice, _intent_failed(invoice))
    _assert_state(books, OPEN)
    assert books["payment_rows"] == 1, books


# ── A, B, A: a late redelivery after a later event for the same charge ─────


def test_success_redelivered_after_a_full_refund_does_not_rebook_the_money(client, db, invoice):
    """The money left; a stale copy of "it arrived" must not bring it back.

    Measured before the fix: the redelivered success saw no LIVE payment (the
    refund voided it: ``_recorded_payment`` excludes voids on purpose, for
    M14), recorded a second $500 row, re-posted cash the cardholder already
    has back, and flipped the invoice to paid. Closed in the handler
    (``_deliberately_voided``), not by the event record, which must not
    swallow M14's re-send (the next test).
    """
    success = _succeeded(invoice)
    _deliver(client, success)
    _deliver(client, _refunded())
    refunded = _books(db, invoice)
    _assert_state(refunded, OPEN)

    out = _deliver(client, success)

    assert _books(db, invoice) == refunded
    assert out["result"]["status"] == "already_reversed", out
    assert out["result"]["voided_reason"] == "charge.refunded", out


def test_a_resent_success_still_recovers_a_payment_a_stale_failure_voided(client, db, invoice, monkeypatch):
    """M14 through the route. A stale ``charge.failed`` that lands while Stripe
    cannot be read voids a good payment; re-sending the same ``succeeded`` is
    the recovery. The first version of this fix recorded ``paid`` and answered
    that re-send with ``duplicate`` (/audit probe, 2026-10-07: money collected,
    invoice open, no way back)."""
    success = _succeeded(invoice)
    _deliver(client, success)

    def unreachable(*a, **k):
        raise RuntimeError("stripe unreachable")

    monkeypatch.setattr("stripe.PaymentIntent.retrieve", unreachable)
    assert _deliver(client, _charge_failed())["result"]["status"] == "reversed"
    _assert_state(_books(db, invoice), OPEN)

    out = _deliver(client, success)

    assert out["result"]["status"] == "paid", out
    _assert_state(_books(db, invoice), PAID)


def test_a_refund_on_a_payment_a_stale_failure_voided_can_still_be_re_sent(client, db, invoice, monkeypatch):
    """Two different reversals on one charge. The refund finds the row already
    voided by the failure and answers ``no_payment_to_reverse``. That is not
    "done" for a refund: M14's re-send brings the payment back, and the refund
    must then still apply. The first version recorded it whenever any row
    existed and swallowed the re-send (/audit probe, 2026-10-07: invoice paid,
    $500 on the books for refunded money)."""
    from gdx_dispatch.models.tenant_models import StripeWebhookEvent

    success = _succeeded(invoice)
    _deliver(client, success)

    def unreachable(*a, **k):
        raise RuntimeError("stripe unreachable")

    monkeypatch.setattr("stripe.PaymentIntent.retrieve", unreachable)
    _deliver(client, _charge_failed())
    refund = _refunded()
    assert _deliver(client, refund)["result"]["status"] == "no_payment_to_reverse"
    assert db.get(StripeWebhookEvent, refund["id"]) is None
    assert _deliver(client, success)["result"]["status"] == "paid"  # M14

    out = _deliver(client, refund)

    assert out["result"]["status"] == "reversed", out
    _assert_state(_books(db, invoice), OPEN)


@pytest.mark.xfail(
    strict=True,
    reason="pre-existing, hypothetical: a verified failure void stores the same reason as "
    "M14's unverified one and no charge id, so a stale success after it re-books",
)
def test_original_success_redelivered_after_a_verified_failure_does_not_rebook(client, db, invoice):
    """HYPOTHETICAL sequence: Stripe documents no failure event on an intent
    that already succeeded (a late ACH failure becomes a dispute, which is
    covered). Here the fixture has Stripe say the failed charge is current, so
    the void is "verified", and the redelivered ORIGINAL success re-books it.
    See ``core.payments._FAILURE_VOID_REASONS``. Strict, so a fix has to flip
    this test, not stumble past it."""
    success = _succeeded(invoice)
    _deliver(client, success)
    _deliver(client, _charge_failed())
    failed = _books(db, invoice)
    _assert_state(failed, OPEN)

    _deliver(client, success)

    assert _books(db, invoice) == failed


def test_success_redelivered_after_a_dispute_does_not_rebook_the_money(client, db, invoice):
    """The dispute's void is not M14's either: the funds are with the bank."""
    success = _succeeded(invoice)
    _deliver(client, success)
    _deliver(client, _dispute("charge.dispute.funds_withdrawn"))
    disputed = _books(db, invoice)
    _assert_state(disputed, OPEN)

    out = _deliver(client, success)

    assert _books(db, invoice) == disputed
    assert out["result"]["status"] == "already_reversed", out


def test_a_real_dispute_lifecycle_redelivered_after_it_was_won_does_not_void_again(client, db, invoice):
    """A won dispute must stay won when either losing-side event arrives again.

    A real (non-inquiry) dispute sends BOTH ``charge.dispute.created`` and
    ``charge.dispute.funds_withdrawn``. ``created`` voids the payment, so the
    withdrawal then finds nothing live and answers ``no_payment_to_reverse``.
    That answer means "already done" here, not "not yet", and it has to be
    recorded: the first version of this fix keyed on the status string alone,
    left it unrecorded, and its redelivery after the win voided the payment
    again (/audit probe, 2026-10-07).
    """
    _pay(client, db, invoice)
    created = _dispute("charge.dispute.created")
    withdrawn = _dispute("charge.dispute.funds_withdrawn")
    assert _deliver(client, created)["result"]["status"] == "reversed"
    assert _deliver(client, withdrawn)["result"]["status"] == "no_payment_to_reverse"
    _deliver(client, _dispute("charge.dispute.funds_reinstated"))
    won = _books(db, invoice)
    _assert_state(won, PAID)

    for late in (withdrawn, created):
        out = _deliver(client, late)
        assert _books(db, invoice) == won, f"a redelivered {late['type']} undid the win"
        assert out["result"]["status"] == "duplicate", out


def test_a_withdrawal_with_no_created_redelivered_after_the_win_does_not_void_again(client, db, invoice):
    """The escalated-inquiry path: only ``funds_withdrawn`` did the void."""
    _pay(client, db, invoice)
    withdrawal = _dispute("charge.dispute.funds_withdrawn")
    _deliver(client, withdrawal)
    _deliver(client, _dispute("charge.dispute.funds_reinstated"))
    won = _books(db, invoice)
    _assert_state(won, PAID)

    out = _deliver(client, withdrawal)

    assert _books(db, invoice) == won
    assert out["result"]["status"] == "duplicate", out


def test_refund_redelivered_after_a_won_dispute_does_not_void_again(client, db, invoice):
    """Same shape through a different branch: the refund path's void."""
    _pay(client, db, invoice)
    _deliver(client, _dispute("charge.dispute.funds_withdrawn"))
    _deliver(client, _dispute("charge.dispute.funds_reinstated"))
    # A full refund that was delivered once, after the win, and is then
    # redelivered: the first copy voids, the second must change nothing even
    # though another reinstatement could have landed in between.
    refund = _refunded()
    _deliver(client, refund)
    refunded = _books(db, invoice)
    _assert_state(refunded, OPEN)

    out = _deliver(client, refund)

    assert _books(db, invoice) == refunded
    assert out["result"]["status"] == "duplicate", out


def test_a_refund_that_arrived_before_its_payment_still_applies_on_redelivery(client, db, invoice):
    """The other direction of out-of-order delivery must not get worse.

    A refund delivered before the success has nothing to void and returns
    ``no_payment_to_reverse``. That is "not yet", not "handled": recording it
    would turn the redelivery that can still apply it into a duplicate.
    """
    from gdx_dispatch.models.tenant_models import StripeWebhookEvent

    refund = _refunded()
    early = _deliver(client, refund)
    assert early["result"]["status"] == "no_payment_to_reverse", early
    assert db.get(StripeWebhookEvent, refund["id"]) is None

    _pay(client, db, invoice)
    late = _deliver(client, refund)

    assert late["result"]["status"] == "reversed", late
    _assert_state(_books(db, invoice), OPEN)


def test_a_reinstatement_that_needed_review_can_be_re_sent_once_fixed(client, db, invoice):
    """A Dashboard re-send is how an operator re-applies an event after fixing
    the data. ``reinstate_needs_review`` is not final, so it must not be
    recorded (/audit probe, 2026-10-07: recorded, the re-send was swallowed as
    a duplicate and the won dispute could never be reinstated)."""
    _pay(client, db, invoice)
    _deliver(client, _dispute("charge.dispute.funds_withdrawn"))
    voided = db.scalars(select(Payment).where(Payment.invoice_id == invoice.id)).one()
    reason = voided.voided_reason
    voided.voided_reason = None  # a void from before migration 076
    db.commit()

    reinstate = _dispute("charge.dispute.funds_reinstated")
    assert _deliver(client, reinstate)["result"]["status"] == "reinstate_needs_review"
    _assert_state(_books(db, invoice), OPEN)

    voided = db.scalars(select(Payment).where(Payment.invoice_id == invoice.id)).one()
    voided.voided_reason = reason  # the office fixes it
    db.commit()
    out = _deliver(client, reinstate)

    assert out["result"]["status"] == "reinstated", out
    _assert_state(_books(db, invoice), PAID)


def test_a_reinstatement_that_arrived_before_its_withdrawal_still_applies_on_redelivery(client, db, invoice):
    """``no_payment_to_reinstate`` on a live payment means "not yet"."""
    _pay(client, db, invoice)
    reinstate = _dispute("charge.dispute.funds_reinstated")
    assert _deliver(client, reinstate)["result"]["status"] == "no_payment_to_reinstate"
    _deliver(client, _dispute("charge.dispute.funds_withdrawn"))
    _assert_state(_books(db, invoice), OPEN)

    out = _deliver(client, reinstate)

    assert out["result"]["status"] == "reinstated", out
    _assert_state(_books(db, invoice), PAID)


# ── the record itself ───────────────────────────────────────────────────────


def test_without_the_table_every_delivery_is_still_handled(client, db, invoice):
    """The rollback path: ``downgrade()`` drops the table, and the route must
    fall back to handling every delivery, not 500 every webhook."""
    from gdx_dispatch.models.tenant_models import StripeWebhookEvent

    StripeWebhookEvent.__table__.drop(bind=db.get_bind())
    db.commit()

    out = _deliver(client, _succeeded(invoice))

    assert out["result"]["status"] == "paid", out
    _assert_state(_books(db, invoice), PAID)


def test_a_failed_delivery_leaves_no_record_so_stripe_can_retry(client, db, invoice, monkeypatch):
    """A 500 must not mark the event handled, or Stripe's retry is dropped."""
    from gdx_dispatch.models.tenant_models import StripeWebhookEvent

    def boom(event, session):
        raise RuntimeError("database went away")

    monkeypatch.setattr("gdx_dispatch.core.payments.handle_payment_webhook", boom)
    event = _succeeded(invoice)
    body = json.dumps(event, separators=(",", ":"))
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    resp = client.post(
        "/stripe/webhook", content=body,
        headers={"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"},
    )
    assert resp.status_code == 500
    assert db.get(StripeWebhookEvent, event["id"]) is None

    monkeypatch.undo()
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(
        "gdx_dispatch.tasks.stale_intent_sweep.enqueue_stale_intent_sweep",
        lambda *a, **k: False,
    )
    out = _deliver(client, event)
    assert out["result"]["status"] == "paid", "the retry was refused"
    _assert_state(_books(db, invoice), PAID)


def test_a_handled_event_is_recorded_with_its_type_and_outcome(client, db, invoice):
    from gdx_dispatch.models.tenant_models import StripeWebhookEvent

    success = _succeeded(invoice)
    _deliver(client, success)
    refund = _refunded()
    _deliver(client, refund)

    db.expire_all()
    row = db.get(StripeWebhookEvent, refund["id"])
    assert row is not None
    assert row.event_type == "charge.refunded"
    assert row.result_status == "reversed"
    assert row.processed_at is not None
    # Never the success: M14 recovers through its re-send.
    assert db.get(StripeWebhookEvent, success["id"]) is None
