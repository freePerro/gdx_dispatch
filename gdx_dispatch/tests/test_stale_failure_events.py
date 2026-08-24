"""A stale failure event must not void money that actually arrived (M14).

Stripe does not guarantee delivery order, and **a card retry reuses the same
PaymentIntent**. So the ordinary sequence —

1. attempt one declines,
2. the customer retries,
3. attempt two succeeds,

— can be delivered as `succeeded` first (recorded, $500) and then the delayed
`charge.failed` from attempt one. That second event reversed the good payment,
re-opened the invoice, and put a customer who had paid in full back into
dunning.

A genuine late ACH return, which **must** reverse, arrives as the same event
shape. What separates them is **the charge**: each attempt on an intent is its
own charge, and a superseded attempt names a charge the intent no longer points
at. An ACH return arrives against the SAME charge that succeeded, so it is not
superseded and still reverses.

Keying on the charge rather than on `PaymentIntent.status` is deliberate. A
status-based rule would have to be right about how Stripe transitions an intent
on a late ACH return in order to be safe — and getting that wrong would
silently stop reversing returned money, which is strictly worse than the bug
being fixed. This rule does not have to know.

The other half of M14 — `_mark_invoice_paid`'s existence check not filtering
`voided_at`, which blocked a redelivered `succeeded` from recovering — was
already fixed; there is a regression test for it at the bottom.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.payments import handle_payment_webhook
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Payment

TENANT = "tenant-m14"
INTENT = "pi_retry_1"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def paid_invoice(db):
    """An invoice with a REAL recorded card payment against `INTENT`."""
    inv = Invoice(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        invoice_number="INV-M14",
        billing_type="standard",
        subtotal=Decimal("500.00"),
        tax_amount=Decimal("0"),
        total=Decimal("500.00"),
        balance_due=Decimal("0.00"),
        status="paid",
        paid_at=datetime.now(UTC),
        invoice_date=datetime.now(UTC).date(),
        public_token=uuid.uuid4().hex,
        company_id=TENANT,
    )
    db.add(inv)
    db.flush()
    db.add(
        InvoiceLine(
            id=uuid.uuid4(), invoice_id=inv.id, description="Opener install",
            quantity=1, unit_price=Decimal("500.00"), line_total=Decimal("500.00"),
            company_id=TENANT,
        )
    )
    db.add(
        Payment(
            id=uuid.uuid4(), invoice_id=inv.id, amount=Decimal("500.00"),
            method="card", reference=INTENT,
            payment_date=datetime.now(UTC).date(), company_id=TENANT,
        )
    )
    db.commit()
    db.refresh(inv)
    return inv


SUCCEEDED_CHARGE = "ch_attempt_2"
DECLINED_CHARGE = "ch_attempt_1"


def _pi(latest_charge: str, status: str = "succeeded"):
    m = MagicMock()
    m.status = status
    m.id = INTENT
    m.latest_charge = latest_charge
    return m


def _live_payments(db, inv):
    return list(
        db.execute(
            select(Payment).where(
                Payment.invoice_id == inv.id, Payment.voided_at.is_(None)
            )
        ).scalars()
    )


def _charge_failed(charge_id):
    return {
        "type": "charge.failed",
        "data": {"object": {"id": charge_id, "payment_intent": INTENT}},
    }


def _pi_failed(latest_charge):
    return {
        "type": "payment_intent.payment_failed",
        "data": {
            "object": {
                "id": INTENT,
                "latest_charge": latest_charge,
                "metadata": {},
                "last_payment_error": {"message": "declined"},
            }
        },
    }


# ── the bug ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("make,key", [(_charge_failed, "status"), (_pi_failed, "reversal")])
def test_a_superseded_decline_does_not_void_the_successful_retry(db, paid_invoice, make, key):
    """THE BUG. Attempt one's decline arrives after attempt two's success.

    The intent has moved on to attempt two's charge, so this event names a
    charge it no longer points at.
    """
    event = make(DECLINED_CHARGE)
    with patch("stripe.PaymentIntent.retrieve", return_value=_pi(SUCCEEDED_CHARGE)):
        out = handle_payment_webhook(event, db)

    assert out[key] == "ignored_stale_failure", out
    assert len(_live_payments(db, paid_invoice)) == 1, "the good payment was voided"
    db.refresh(paid_invoice)
    assert paid_invoice.status == "paid"
    assert float(paid_invoice.balance_due or 0) == 0.0


# ── what must STILL reverse ────────────────────────────────────────────────


@pytest.mark.parametrize("make", [_charge_failed, _pi_failed])
def test_a_failure_on_the_CURRENT_charge_still_reverses(db, paid_invoice, make):
    """The counterfactual, and the one that matters most.

    A failure naming the charge the intent still points at is the live attempt
    failing — that money did not stay, and recording only its arrival is how
    books drift: the invoice reads paid, dunning stops chasing, the cash is
    gone.

    The intent is left `succeeded` here on purpose. If this rule keyed on
    status instead of the charge, THIS is the test that would fail — which is
    why the rule keys on the charge.

    An earlier version of this test called the scenario "an ACH return". That
    was wrong: a `us_bank_account` failure after the intent reaches `succeeded`
    raises a DISPUTE, not these events (see the dispute tests below). The
    assertion is sound; only the story about it was fiction.
    """
    event = make(SUCCEEDED_CHARGE)
    with patch("stripe.PaymentIntent.retrieve", return_value=_pi(SUCCEEDED_CHARGE, status="succeeded")):
        handle_payment_webhook(event, db)

    assert _live_payments(db, paid_invoice) == [], "a real return was not reversed"
    db.refresh(paid_invoice)
    assert paid_invoice.status == "sent", "the invoice must re-enter aging"
    assert float(paid_invoice.balance_due or 0) == 500.00


@pytest.mark.parametrize("make", [_charge_failed, _pi_failed])
def test_an_unreadable_status_reverses_and_shouts(db, paid_invoice, make, caplog):
    """When Stripe cannot be reached we cannot tell the two apart.

    Fall back to today's behaviour — reverse — rather than inventing a new
    failure mode where genuinely returned money silently stays recorded. But
    say so loudly: this is a decision made without evidence.
    """
    with patch("stripe.PaymentIntent.retrieve", side_effect=RuntimeError("stripe down")):
        handle_payment_webhook(make(DECLINED_CHARGE), db)

    assert _live_payments(db, paid_invoice) == []
    assert "failure_event_unverified" in caplog.text


def test_a_dispute_still_reverses_even_though_the_intent_succeeded(db, paid_invoice):
    """A dispute is NOT a stale-ordering problem — the intent stays
    `succeeded` while the money is held. Routing disputes through the M14
    check would refuse every one of them.
    """
    event = {
        "type": "charge.dispute.created",
        "data": {"object": {"payment_intent": INTENT}},
    }
    with patch("stripe.PaymentIntent.retrieve", return_value=_pi(SUCCEEDED_CHARGE)) as r:
        handle_payment_webhook(event, db)

    r.assert_not_called()
    assert _live_payments(db, paid_invoice) == [], "a dispute must still reverse"


# ── M14's other half, already fixed — pinned so it stays fixed ─────────────


def test_a_redelivered_success_can_recover_a_wrongly_voided_payment(db, paid_invoice):
    """`_mark_invoice_paid`'s existence check must exclude VOIDED rows.

    Without that filter a wrongly-reversed payment left a voided row here, the
    redelivered `succeeded` saw it and returned early, and the invoice stayed
    open with the money collected.
    """
    payment = db.execute(select(Payment).where(Payment.reference == INTENT)).scalars().one()
    payment.voided_at = datetime.now(UTC)
    db.commit()

    out = handle_payment_webhook(
        {
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "id": INTENT,
                    "metadata": {"invoice_id": str(paid_invoice.id)},
                    "currency": "usd",
                    "amount_received": 50000,
                }
            },
        },
        db,
    )

    assert out["status"] == "paid", out
    assert len(_live_payments(db, paid_invoice)) == 1, "recovery was blocked"


def test_a_failure_event_naming_no_charge_reverses_rather_than_guessing(db, paid_invoice):
    """Nothing to compare means we do not know, and unknown is not "stale".

    A direct charge with no PaymentIntent, or an event shape that carries no
    charge id, must not be silently treated as superseded — that is how a real
    reversal gets dropped.
    """
    with patch("stripe.PaymentIntent.retrieve", return_value=_pi(SUCCEEDED_CHARGE)) as r:
        handle_payment_webhook(_charge_failed(""), db)

    r.assert_not_called()
    assert _live_payments(db, paid_invoice) == [], "an unknown must still reverse"


def test_the_rule_keys_on_the_charge_not_the_intent_status(db, paid_invoice):
    """Names the discriminator, so a later 'simplification' to
    `status == "succeeded"` is a visible change rather than a quiet one.

    The two disagree exactly where it matters: an ACH return leaves the intent
    succeeded AND names the current charge. Status says "skip"; charge says
    "reverse". Charge is right.
    """
    from gdx_dispatch.core import payments as mod

    assert hasattr(mod, "_failure_event_is_superseded")
    assert not hasattr(mod, "_intent_is_currently_succeeded"), (
        "the status-based helper is back — it silently stops reversing "
        "returned ACH money"
    )


# ── disputes: the money hole the M14 review actually found ─────────────────


def _dispute(status):
    return {
        "type": "charge.dispute.created",
        "data": {
            "object": {
                "id": "dp_1",
                "payment_intent": INTENT,
                "status": status,
                "amount": 50000,
            }
        },
    }


@pytest.mark.parametrize("status", ["warning_needs_response", "warning_under_review", "warning_closed"])
def test_a_dispute_INQUIRY_does_not_void_a_real_payment(db, paid_invoice, status):
    """Stripe's `warning_*` dispute statuses are INQUIRIES — the bank is asking
    a question and **no funds have been withdrawn**. ACH raises these
    routinely.

    Voiding a real payment over a paperwork request re-opens the invoice, puts
    a customer who paid back into dunning, and books cash as gone that is still
    sitting there. Found by the adversarial review of M14, which noted there
    was no `dispute.status` branching anywhere in the module.
    """
    out = handle_payment_webhook(_dispute(status), db)

    assert out["status"] == "dispute_inquiry_noted", out
    assert len(_live_payments(db, paid_invoice)) == 1, "an inquiry voided real money"
    db.refresh(paid_invoice)
    assert paid_invoice.status == "paid"


@pytest.mark.parametrize("status", ["needs_response", "under_review", "lost"])
def test_a_REAL_dispute_still_reverses(db, paid_invoice, status):
    """The counterfactual. Once funds are actually withdrawn the payment has to
    come back off — that is the whole point of handling disputes at all."""
    handle_payment_webhook(_dispute(status), db)

    assert _live_payments(db, paid_invoice) == [], "a real dispute did not reverse"
    db.refresh(paid_invoice)
    assert paid_invoice.status == "sent"


def test_a_dispute_with_no_status_reverses_rather_than_guessing(db, paid_invoice):
    """Absent is not "inquiry". An event shape we cannot read must fall back to
    the conservative behaviour — reverse — not to silently keeping the money on
    the books."""
    event = _dispute("warning_needs_response")
    del event["data"]["object"]["status"]

    handle_payment_webhook(event, db)
    assert _live_payments(db, paid_invoice) == []


# ── M15: the dispute lifecycle has two ends ────────────────────────────────
#
# `charge.dispute.created` voided the payment and NOTHING handled the other
# end. Winning a dispute reinstates the money at Stripe; here the payment
# stayed voided, the invoice stayed open, and dunning chased a customer whose
# charge had stood.
#
# Stripe publishes the money movement itself, and these are the only two
# events that mean cash actually moved because of a dispute (docs, verified
# 2026-08-23):
#   charge.dispute.funds_withdrawn  — "funds are removed from your account"
#   charge.dispute.funds_reinstated — "funds are reinstated to your account"


def _funds(kind):
    return {
        "type": f"charge.dispute.funds_{kind}",
        "data": {"object": {"id": "dp_l", "payment_intent": INTENT, "amount": 50000}},
    }


def test_winning_a_dispute_puts_the_payment_back(db, paid_invoice):
    """THE M15 BUG. The money stood; the books said otherwise."""
    handle_payment_webhook(_funds("withdrawn"), db)
    assert _live_payments(db, paid_invoice) == [], "setup: the withdrawal reverses"
    db.refresh(paid_invoice)
    assert paid_invoice.status == "sent"

    out = handle_payment_webhook(_funds("reinstated"), db)

    assert out["status"] == "reinstated", out
    assert len(_live_payments(db, paid_invoice)) == 1, "the money never came back"
    db.refresh(paid_invoice)
    assert float(paid_invoice.balance_due or 0) == 0.0, "the invoice stayed open"
    # Balance 0 with status "sent" would be a lying invoice — settled money
    # under an unsettled label, still in aging and still being dunned.
    assert paid_invoice.status == "paid", (
        f"balance is 0 but status is {paid_invoice.status!r} — a lying status"
    )


def test_an_escalating_inquiry_is_caught_by_the_withdrawal(db, paid_invoice):
    """The gap the inquiry guard opens, and why funds_withdrawn closes it.

    An inquiry is correctly NOT reversed on `created`. But it can escalate —
    and when it does, the money leaves. `charge.dispute.created` alone could
    never see that, because it already fired.
    """
    handle_payment_webhook(_dispute("warning_needs_response"), db)
    assert len(_live_payments(db, paid_invoice)) == 1, "setup: an inquiry does not reverse"

    handle_payment_webhook(_funds("withdrawn"), db)

    assert _live_payments(db, paid_invoice) == [], "the escalation was missed"
    db.refresh(paid_invoice)
    assert paid_invoice.status == "sent"


def test_reinstating_does_not_invent_a_second_payment(db, paid_invoice):
    """Un-void, never insert. A new row would double-count against
    `core/invoice_paid.py`, which sums non-voided payments — the invoice would
    read as paid twice over."""
    handle_payment_webhook(_funds("withdrawn"), db)
    handle_payment_webhook(_funds("reinstated"), db)

    every = list(
        db.execute(select(Payment).where(Payment.invoice_id == paid_invoice.id)).scalars()
    )
    assert len(every) == 1, f"{len(every)} payment rows for one charge"
    assert every[0].voided_at is None


def test_reinstating_when_nothing_was_reversed_is_not_an_error(db, paid_invoice):
    """An inquiry we correctly declined to reverse can still close in our
    favour. There is nothing to put back, and that is a normal outcome — not a
    failure, and above all not a second payment."""
    out = handle_payment_webhook(_funds("reinstated"), db)

    assert out["status"] == "no_payment_to_reinstate", out
    assert len(_live_payments(db, paid_invoice)) == 1


def test_a_stripe_refunds_void_is_never_reinstated_by_a_dispute(db, paid_invoice):
    """THE hole an adversarial review proved, driven through the REAL path.

    Three things void a payment: a dispute reversal, a **full Stripe refund**,
    and the office's own void action. With only `voided_at` to go on, a
    dispute reinstatement un-voided whichever it found — so a refunded payment
    came back and the invoice read paid on cash that had been returned.

    An earlier version of this test built the refund by hand as an office-side
    `InvoiceAdjustment`, and an earlier version of the guard checked for one.
    Neither touched the path that matters: `_apply_charge_refund`'s full-refund
    branch is a bare `return _reverse_recorded_payment(...)` and writes NO
    adjustment. The guard was checking for a row that path never creates, and
    the test agreed with it. This drives `charge.refunded` through the webhook
    instead.
    """
    refund = {
        "type": "charge.refunded",
        "data": {
            "object": {
                "id": "ch_x", "payment_intent": INTENT,
                "amount": 50000, "amount_refunded": 50000, "refunded": True,
            }
        },
    }
    handle_payment_webhook(refund, db)
    assert _live_payments(db, paid_invoice) == [], "setup: a full refund voids the payment"
    voided = db.execute(select(Payment).where(Payment.reference == INTENT)).scalars().one()
    assert voided.voided_reason == "charge.refunded", voided.voided_reason

    out = handle_payment_webhook(_funds("reinstated"), db)

    assert out["status"] == "reinstate_needs_review", out
    assert _live_payments(db, paid_invoice) == [], "money was invented on refunded cash"


def test_an_office_void_is_never_reinstated_by_a_dispute(db, paid_invoice):
    """The office reversed this payment on purpose. A dispute closing in our
    favour months later must not quietly undo their decision."""
    payment = db.execute(select(Payment).where(Payment.reference == INTENT)).scalars().one()
    payment.voided_at = datetime.now(UTC)
    payment.voided_reason = "office_void"
    db.commit()

    out = handle_payment_webhook(_funds("reinstated"), db)

    assert out["status"] == "reinstate_needs_review", out
    assert _live_payments(db, paid_invoice) == []


def test_a_void_from_before_the_reason_was_recorded_is_treated_as_unknown(db, paid_invoice):
    """Migration 076 backfills nothing — what nobody recorded cannot be
    reconstructed. NULL is UNKNOWN, and unknown must never mean "safe to
    reverse". Prod carries 3 such rows."""
    payment = db.execute(select(Payment).where(Payment.reference == INTENT)).scalars().one()
    payment.voided_at = datetime.now(UTC)
    payment.voided_reason = None
    db.commit()

    out = handle_payment_webhook(_funds("reinstated"), db)

    assert out["status"] == "reinstate_needs_review", out
    assert "unrecorded" in out["why"]
    assert _live_payments(db, paid_invoice) == []


def test_reinstating_refuses_when_two_payments_were_voided(db, paid_invoice):
    """Ambiguity is not a tie to break — it is a question for a human."""
    handle_payment_webhook(_funds("withdrawn"), db)
    db.add(
        Payment(
            id=uuid.uuid4(), invoice_id=paid_invoice.id, amount=Decimal("500.00"),
            method="card", reference=INTENT, voided_at=datetime.now(UTC),
            payment_date=datetime.now(UTC).date(), company_id=TENANT,
        )
    )
    db.commit()

    out = handle_payment_webhook(_funds("reinstated"), db)

    assert out["status"] == "reinstate_needs_review", out
    assert _live_payments(db, paid_invoice) == []


def test_reinstating_does_not_move_the_invoice_into_a_later_period(db, paid_invoice):
    """The invoice was paid when the customer paid it, not when a dispute
    closed months later.

    `_recalculate_invoice` re-stamps `paid_at` on the flip back to paid, which
    would move this invoice's revenue into the wrong period on every report
    that groups by it — a silent restatement of a closed month. Measured by an
    adversarial review before it was fixed.
    """
    # A payment taken in JUNE, disputed and resolved in AUGUST. Asserting on
    # a real gap rather than on microsecond equality: the question is which
    # PERIOD the revenue lands in, and a same-instant fixture could not tell
    # a preserved date from a re-stamped one.
    june = datetime(2026, 6, 15, 14, 30, tzinfo=UTC)
    payment = db.execute(select(Payment).where(Payment.reference == INTENT)).scalars().one()
    payment.created_at = june
    payment.payment_date = june.date()
    paid_invoice.paid_at = june
    db.commit()

    handle_payment_webhook(_funds("withdrawn"), db)
    handle_payment_webhook(_funds("reinstated"), db)

    db.refresh(paid_invoice)
    assert paid_invoice.status == "paid"
    assert paid_invoice.paid_at is not None
    assert paid_invoice.paid_at.month == 6, (
        f"paid_at landed in month {paid_invoice.paid_at.month}, not June — the "
        "revenue moved into the dispute's period and silently restated a "
        "closed month"
    )


@pytest.mark.parametrize("kind,action", [("withdrawn", "payment_reversed"), ("reinstated", "payment_reinstated")])
def test_both_directions_leave_an_audit_row(db, paid_invoice, kind, action):
    """Invariant #1: who did it, what changed, when. Both of these move money
    on an invoice off a signed Stripe event, and both used to leave nothing but
    a `logger.warning` — which is not a record, it is a hope that somebody
    greps.

    The actor is named as the webhook, because it IS machine-initiated;
    attributing it to whoever last logged in would be worse than saying so.
    """
    from gdx_dispatch.core.audit import AuditLog

    if kind == "reinstated":
        handle_payment_webhook(_funds("withdrawn"), db)
    handle_payment_webhook(_funds(kind), db)

    row = (
        db.query(AuditLog)
        .filter(AuditLog.action == action)
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert row is not None, f"{action} left no audit row"
    assert row.user_id == "stripe-webhook"
    assert row.details["amount"] == 500.0
    assert row.details["stripe_event"] == f"charge.dispute.funds_{kind}"

