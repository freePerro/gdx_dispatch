"""A stale PaymentIntent must not collect money the invoice no longer owes (M12).

A PaymentIntent **freezes its amount when it is minted**, and `/confirm` only
*records* what Stripe.js already charged in the browser — by the time the server
hears about it the money has moved. So there is no server-side veto at charge
time, and the window is real:

    balance $500 → customer opens the pay page (intent minted at $500)
    → office records a $300 check → the customer's still-open tab confirms
    → **$800 collected on a $500 invoice.**

The fix has two halves.

1. **Close the open tab** when money arrives another way. Stripe cancels from
   `requires_payment_method` / `requires_confirmation` / `requires_action` /
   `requires_capture` (<https://docs.stripe.com/api/payment_intents/cancel>,
   read 2026-08-24) — exactly the "customer sitting on the page" states.

   **Stripe is the register, not us.** Every mint site already stamps
   `metadata.invoice_id` (the same binding `/confirm` checks before recording a
   payment), so `PaymentIntent.list` answers "what is open on this invoice"
   completely, including from mint sites nobody remembered to wire. An earlier
   draft of this fix kept a local `payment_intent_mints` table; it needed four
   mint sites wired by hand, a Connect column, a retention policy, and it
   marked intents permanently "handled" when a cancel failed for a transport
   reason. All of that is gone with the table.

   `list`, not `search`: Search data is only "searchable in under 1 minute"
   (<https://docs.stripe.com/search>, read 2026-08-24) and this bug lives
   inside that minute.

2. **Say so when it happens anyway.** ACH in `processing` cannot be stopped —
   the money is already moving. The money is recorded in full (discarding it
   would be a different lie) and a `payment_exceeds_receivable` audit event
   makes the excess searchable instead of visible on one screen only.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase, ensure_audit_table
from gdx_dispatch.core.payments import (
    _create_usable_intent,
    _mark_invoice_paid,
    cancel_open_intents_for_invoice,
)
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Payment

TENANT = "tenant-m12"


def _pi(pid, status, invoice_id, amount=50000, created=1_700_000_000):
    """A PaymentIntent shaped the way Stripe's list endpoint returns one."""
    return SimpleNamespace(
        id=pid, status=status, amount=amount, created=created,
        metadata={"invoice_id": str(invoice_id)},
    )


def _page(rows, has_more=False):
    return SimpleNamespace(data=rows, has_more=has_more)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    ensure_audit_table(session)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def invoice(db):
    """A $500 invoice with $300 already paid — $200 still owed."""
    inv = Invoice(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        invoice_number="INV-M12",
        billing_type="standard",
        subtotal=Decimal("500.00"),
        tax_amount=Decimal("0"),
        total=Decimal("500.00"),
        balance_due=Decimal("200.00"),
        status="sent",
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
            id=uuid.uuid4(), invoice_id=inv.id, amount=Decimal("300.00"),
            method="check", reference="chk-1",
            payment_date=datetime.now(UTC).date(), company_id=TENANT,
        )
    )
    db.commit()
    db.refresh(inv)
    return inv


@pytest.fixture
def unpaid_invoice(db):
    """A $400 invoice with nothing recorded against it.

    `void_invoice` refuses an invoice that has recorded payments, so this is
    the only shape a void can ever act on — and it is exactly the shape where a
    customer can still have the pay page open.
    """
    inv = Invoice(
        id=uuid.uuid4(), customer_id=uuid.uuid4(), job_id=uuid.uuid4(),
        invoice_number="INV-M12-VOID", billing_type="standard",
        subtotal=Decimal("400.00"), tax_amount=Decimal("0"), total=Decimal("400.00"),
        balance_due=Decimal("400.00"), status="sent",
        invoice_date=datetime.now(UTC).date(), public_token=uuid.uuid4().hex,
        company_id=TENANT,
    )
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(
        id=uuid.uuid4(), invoice_id=inv.id, description="Spring replacement",
        quantity=1, unit_price=Decimal("400.00"), line_total=Decimal("400.00"),
        company_id=TENANT,
    ))
    db.commit()
    db.refresh(inv)
    return inv


# ── closing the open tab ───────────────────────────────────────────────────


def test_the_customers_open_tab_is_cancelled(invoice):
    """THE FIX. The stale intent is closed before it can collect a second time."""
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_open", "requires_payment_method", invoice.id),
    ])), patch("stripe.PaymentIntent.cancel") as cancel:
        out = cancel_open_intents_for_invoice(invoice, why="payment_recorded", remaining_cents=0)

    cancel.assert_called_once()
    assert cancel.call_args[0][0] == "pi_open"
    # "duplicate", not "abandoned": the invoice was settled another way, so a
    # second collection would be a duplicate — the customer did not walk away.
    assert cancel.call_args[1]["cancellation_reason"] == "duplicate"
    assert out == [{"intent_id": "pi_open", "result": "canceled"}]


@pytest.mark.parametrize(
    "status", ["requires_payment_method", "requires_confirmation",
               "requires_action", "requires_capture"],
)
def test_every_state_stripe_can_cancel_from_is_cancelled(invoice, status):
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_x", status, invoice.id),
    ])), patch("stripe.PaymentIntent.cancel") as cancel:
        cancel_open_intents_for_invoice(invoice, why="payment_recorded", remaining_cents=0)
    cancel.assert_called_once()


@pytest.mark.parametrize("status", ["succeeded", "canceled"])
def test_a_settled_intent_is_left_alone(invoice, status):
    """A succeeded intent is real money being recorded elsewhere; a cancelled
    one is already done. Touching either would be noise at best."""
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_done", status, invoice.id),
    ])), patch("stripe.PaymentIntent.cancel") as cancel:
        out = cancel_open_intents_for_invoice(invoice, why="payment_recorded", remaining_cents=0)
    cancel.assert_not_called()
    assert out == []


def test_an_in_flight_ach_that_would_overcharge_is_attempted_not_excused(invoice, caplog):
    """Stripe permits cancelling `processing` for the bank-debit family — ACH,
    ACSS, AU BECS, BACS, NZ BECS, SEPA — though "cancellation might fail due to
    a limited and varying cancellation time window"
    (docs.stripe.com/payments/paymentintents/lifecycle, read 2026-08-24).

    A first version skipped `processing` outright and logged that the money
    "cannot be cancelled". That was wrong on the docs, and refusing to try
    guaranteed the overcharge it was reporting.
    """
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_ach", "processing", invoice.id, amount=50000),
    ])), patch("stripe.PaymentIntent.cancel") as cancel:
        out = cancel_open_intents_for_invoice(invoice, why="x", remaining_cents=0)

    cancel.assert_called_once()
    assert cancel.call_args[0][0] == "pi_ach"
    assert out == [{"intent_id": "pi_ach", "result": "canceled"}]


def test_an_ach_past_its_cancellation_window_is_reported_as_in_flight(invoice, caplog):
    """When Stripe refuses because the debit is already moving, say so — and say
    that it will overcharge on settlement, which is what the backstop is for."""
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_ach", "processing", invoice.id, amount=50000),
    ])), patch("stripe.PaymentIntent.cancel",
               side_effect=RuntimeError("cancellation window has passed")), \
            caplog.at_level("ERROR"):
        out = cancel_open_intents_for_invoice(invoice, why="x", remaining_cents=0)

    assert out == [{"intent_id": "pi_ach", "result": "in_flight"}]
    assert "stale_intent_in_flight_uncancellable" in caplog.text



def test_another_invoices_open_tab_is_not_touched(invoice):
    """The scope is one invoice. Cancelling somebody else's live checkout
    because THIS invoice got paid would be far worse than the bug being fixed.

    This is the test that would have caught a `metadata.invoice_id` filter
    applied to the wrong field — the list call returns the whole account.
    """
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_theirs", "requires_payment_method", uuid.uuid4()),
        _pi("pi_ours", "requires_payment_method", invoice.id),
    ])), patch("stripe.PaymentIntent.cancel") as cancel:
        out = cancel_open_intents_for_invoice(invoice, why="payment_recorded", remaining_cents=0)

    assert [c[0][0] for c in cancel.call_args_list] == ["pi_ours"]
    assert out == [{"intent_id": "pi_ours", "result": "canceled"}]


def test_a_cancel_that_fails_says_the_intent_is_still_live(invoice, caplog):
    """No state is written either way, so the honest report is "still open"."""
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_open", "requires_payment_method", invoice.id),
    ])), patch("stripe.PaymentIntent.cancel", side_effect=RuntimeError("boom")), \
            caplog.at_level("ERROR"):
        out = cancel_open_intents_for_invoice(invoice, why="payment_recorded", remaining_cents=0)

    assert out == [{"intent_id": "pi_open", "result": "failed"}]
    assert "can still collect" in caplog.text


def test_stripe_being_unreachable_never_breaks_the_payment(invoice):
    """The payment is already committed by the time this runs. A sweep that
    cannot happen must not cost the recorded payment — and, because nothing is
    persisted, it also leaves nothing behind that is now wrong. The next settle
    event simply asks Stripe again.
    """
    with patch("stripe.PaymentIntent.list", side_effect=RuntimeError("no route to host")):
        out = cancel_open_intents_for_invoice(invoice, why="payment_recorded", remaining_cents=0)
    assert out == []


def test_the_scan_window_is_bounded_and_says_when_it_truncates(invoice, caplog):
    """A silent cap reads as "we checked everything". This one says so."""
    full = [_pi(f"pi_{i}", "succeeded", uuid.uuid4()) for i in range(100)]
    with patch("stripe.PaymentIntent.list", return_value=_page(full, has_more=True)) as lst, \
            patch("stripe.PaymentIntent.cancel"), caplog.at_level("ERROR"):
        cancel_open_intents_for_invoice(invoice, why="payment_recorded", remaining_cents=0)

    assert lst.call_count == 5, "the page walk must stop rather than spin"
    assert "stale_intent_scan_truncated" in caplog.text


def test_the_scan_is_scoped_to_a_bounded_window(invoice):
    with patch("stripe.PaymentIntent.list", return_value=_page([])) as lst:
        cancel_open_intents_for_invoice(invoice, why="payment_recorded", remaining_cents=0)
    created = lst.call_args[1]["created"]
    assert "gte" in created, "an unbounded list would walk the whole account"


# ── the call sites are actually wired ──────────────────────────────────────
#
# The audit's sharpest finding on the first draft: every test passed with the
# calls deleted. These drive the real handlers and assert the sweep is queued.

OFFICE = {"id": "office-user", "email": "office@example.com", "role": "admin"}


def test_recording_an_office_payment_queues_the_sweep(db, invoice):
    """The office records a $50 check against an invoice owing $200.

    $150 is still owed, so this is NOT a settled invoice — and that is the
    point. The customer's open tab froze its amount at $200 when it was minted;
    if they now confirm it they pay $200 against a $150 balance. The sweep is
    queued with the NEW remaining balance so it cancels exactly the intents
    that would overcharge and leaves any smaller one alone.
    """
    from gdx_dispatch.routers.invoices import PaymentCreateIn, record_payment

    with patch("gdx_dispatch.routers.invoices.enqueue_stale_intent_sweep") as sweep:
        record_payment(
            invoice_id=invoice.id,
            payload=PaymentCreateIn(amount=50.0, method="check", date=date.today()),
            _=OFFICE, db=db,
        )

    sweep.assert_called_once()
    assert sweep.call_args[1]["why"] == "payment_recorded"
    assert sweep.call_args[1].get("settled") in (None, False), (
        "a partial payment does not settle the invoice"
    )


def test_issuing_a_credit_memo_queues_the_sweep(db, invoice):
    """A credit settles part of the bill too, so it must close stale tabs."""
    from gdx_dispatch.routers.invoices import CreditMemoIn, issue_credit_memo

    with patch("gdx_dispatch.routers.invoices.enqueue_stale_intent_sweep") as sweep:
        issue_credit_memo(
            str(invoice.id), CreditMemoIn(amount=50.0, reason="goodwill"),
            db=db, _=OFFICE,
        )
    sweep.assert_called_once()
    assert sweep.call_args[1]["why"] == "credit_memo_issued"


def test_applying_customer_credit_queues_the_sweep(db, invoice):
    """The GL preconditions are patched, not seeded: what is under test is that
    the sweep is reached at the end of this handler, not the credit
    arithmetic, which `test_gl_adjustment_posting.py` owns."""
    from gdx_dispatch.routers.invoices import ApplyCreditIn, apply_customer_credit

    with patch("gdx_dispatch.routers.invoices.ledger_posting_enabled", return_value=True), \
            patch("gdx_dispatch.routers.invoices.customer_credit_balance_cents", return_value=100_000), \
            patch("gdx_dispatch.routers.invoices.post_credit_application"), \
            patch("gdx_dispatch.routers.invoices.enqueue_stale_intent_sweep") as sweep:
        apply_customer_credit(invoice.id, ApplyCreditIn(amount=25.0), db=db, _=OFFICE)
    sweep.assert_called_once()
    assert sweep.call_args[1]["why"] == "customer_credit_applied"


def test_voiding_an_invoice_queues_the_sweep_with_nothing_owed(db, unpaid_invoice):
    """The call site an adversarial review found missing.

    A void is the one event after which the invoice can NEVER owe anything
    again, so every open intent on it is stale by definition —
    `remaining_cents=0` means each one exceeds what is owed. Without this, a
    voided invoice whose customer still had the pay page open could be paid,
    and the webhook would book that payment onto a void whose parts and change
    orders had already gone back to the unbilled checklist.
    """
    from gdx_dispatch.routers.invoices import void_invoice

    with patch("gdx_dispatch.routers.invoices.enqueue_stale_intent_sweep") as sweep:
        void_invoice(unpaid_invoice.id, _=OFFICE, db=db)

    sweep.assert_called_once()
    assert sweep.call_args[1]["why"] == "invoice_voided"
    assert sweep.call_args[1]["settled"] is True, (
        "a void is the one event after which nothing can ever be owed"
    )


# ── the sweep runs on a task, not in the money transaction ─────────────────


def test_the_sweep_module_is_in_the_workers_include_list():
    """A task module missing from `include` is never imported by the worker, so
    the task is never registered — shipped and silently never run.

    Asserting on `celery_app.tasks` does NOT catch that: this test file imports
    the module itself, so the decorator registers the task regardless of the
    config. A counterfactual proved that version vacuous — deleting the include
    entry failed nothing. `conf.include` is what the worker actually reads.
    """
    from gdx_dispatch.core.celery_app import celery_app

    assert "gdx_dispatch.tasks.stale_intent_sweep" in (celery_app.conf.include or []), (
        "the worker will never import this module, so the sweep never runs"
    )


def test_the_sweep_runs_on_the_high_priority_queue():
    """The window this closes is the seconds a customer has the page open. A
    low-priority queue behind a nightly job would miss it."""
    from gdx_dispatch.core.celery_app import celery_app

    celery_app.loader.import_default_modules()
    assert celery_app.tasks["payments.sweep_stale_intents"].queue == "priority:high"


def test_queueing_never_waits_on_the_result_backend():
    """Measured: `.delay()` against an unreachable Redis took ~19s, and it was
    the RESULT BACKEND retrying, not the broker. That latency lands directly in
    `record_payment` and the Stripe webhook — the thing this task exists to
    keep out of them. Nothing reads the return value, so nothing should store
    it.
    """
    from gdx_dispatch.tasks.stale_intent_sweep import sweep_stale_intents

    assert sweep_stale_intents.ignore_result is True


def test_queueing_does_not_retry_the_connection(invoice):
    from gdx_dispatch.tasks import stale_intent_sweep as mod

    with patch.object(mod.sweep_stale_intents, "apply_async") as send:
        mod.enqueue_stale_intent_sweep(invoice, why="payment_recorded", settled=True)
    assert send.call_args[1]["retry"] is False, (
        "a broker hiccup must cost this call milliseconds, not seconds"
    )


def test_the_task_sweeps_the_invoice_it_was_given(db, invoice):
    from gdx_dispatch.tasks.stale_intent_sweep import sweep_stale_intents

    with patch("gdx_dispatch.tasks.stale_intent_sweep.SessionLocal") as SL, \
            patch("gdx_dispatch.core.payments.cancel_open_intents_for_invoice",
                  return_value=[{"intent_id": "pi_x", "result": "canceled"}]) as sweep:
        SL.return_value.__enter__.return_value = db
        out = sweep_stale_intents(str(invoice.id), why="payment_recorded")

    assert out["results"] == [{"intent_id": "pi_x", "result": "canceled"}]
    assert sweep.call_args[1]["remaining_cents"] == 20000


def test_a_dead_broker_never_costs_the_payment(invoice):
    """The payment is already committed when this runs. A queue that cannot be
    reached must not surface as a failed payment."""
    from gdx_dispatch.tasks import stale_intent_sweep as mod

    with patch.object(mod.sweep_stale_intents, "apply_async", side_effect=RuntimeError("no broker")):
        assert mod.enqueue_stale_intent_sweep(
            invoice, why="payment_recorded", settled=True
        ) is False


def test_a_vanished_invoice_is_reported_not_crashed(db):
    from gdx_dispatch.tasks.stale_intent_sweep import sweep_stale_intents

    with patch("gdx_dispatch.tasks.stale_intent_sweep.SessionLocal") as SL:
        SL.return_value.__enter__.return_value = db
        out = sweep_stale_intents(str(uuid.uuid4()), why="payment_recorded")
    assert out["error"] == "invoice_not_found"


# ── only intents that would OVERCHARGE are cancelled ───────────────────────


def test_an_intent_that_cannot_overcharge_is_left_alone(invoice):
    """A $100 deposit intent against an invoice still owing $150 is not stale.

    Killing a customer's live checkout that was never going to overcharge them
    is a worse bug than the one being fixed.
    """
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_deposit", "requires_payment_method", invoice.id, amount=10000),
    ])), patch("stripe.PaymentIntent.cancel") as cancel:
        out = cancel_open_intents_for_invoice(
            invoice, why="payment_recorded", remaining_cents=15000
        )
    cancel.assert_not_called()
    assert out == []


def test_an_intent_that_would_overcharge_is_cancelled(invoice):
    """The same $200 intent, after a $50 check leaves $150 owed."""
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_stale", "requires_payment_method", invoice.id, amount=20000),
    ])), patch("stripe.PaymentIntent.cancel") as cancel:
        out = cancel_open_intents_for_invoice(
            invoice, why="payment_recorded", remaining_cents=15000
        )
    cancel.assert_called_once()
    assert out == [{"intent_id": "pi_stale", "result": "canceled"}]


def test_an_exact_match_is_left_alone(invoice):
    """Boundary: an intent for exactly what is owed is the customer paying
    their bill. Cancelling it would break ordinary payment."""
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_exact", "requires_payment_method", invoice.id, amount=15000),
    ])), patch("stripe.PaymentIntent.cancel") as cancel:
        cancel_open_intents_for_invoice(invoice, why="x", remaining_cents=15000)
    cancel.assert_not_called()


# ── Connect: look where the object actually lives ──────────────────────────


def test_a_connected_account_is_scanned_and_cancelled_on(invoice):
    """A scan of the platform account for an intent minted on a connected
    account returns nothing — which is indistinguishable from "all clear"."""
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_connect", "requires_payment_method", invoice.id),
    ])) as lst, patch("stripe.PaymentIntent.cancel") as cancel:
        cancel_open_intents_for_invoice(
            invoice, why="payment_recorded", remaining_cents=0,
            connected_account="acct_123",
        )
    assert lst.call_args[1]["stripe_account"] == "acct_123"
    assert cancel.call_args[1]["stripe_account"] == "acct_123"


def test_no_connected_account_means_the_platform_account(invoice):
    with patch("stripe.PaymentIntent.list", return_value=_page([])) as lst:
        cancel_open_intents_for_invoice(invoice, why="x", remaining_cents=0)
    assert "stripe_account" not in lst.call_args[1]


# ── the mint sites keep their end of the bargain ───────────────────────────


def test_a_minted_intent_carries_the_invoice_binding(invoice):
    """The whole design rests on this: `list` can only find an intent by
    `metadata.invoice_id`. A mint site that stops stamping it becomes
    invisible to the sweep — silently, and only for that one path.
    """
    from gdx_dispatch.core.payments import _create_usable_intent

    with patch("stripe.PaymentIntent.create", return_value=_pi("pi_new", "requires_payment_method", invoice.id)) as create:
        _create_usable_intent(
            amount=20000, currency="usd",
            metadata={"invoice_id": str(invoice.id)},
            idempotency_key="k",
        )
    assert create.call_args[1]["metadata"]["invoice_id"] == str(invoice.id)



def test_a_healthy_intent_is_not_re_minted(invoice):
    """The counterfactual: if this failed we would double-mint every payment."""
    from gdx_dispatch.core.payments import _create_usable_intent

    with patch("stripe.PaymentIntent.create",
               return_value=_pi("pi_ok", "requires_payment_method", invoice.id)) as create:
        _create_usable_intent(
            amount=20000, currency="usd", metadata={}, idempotency_key="k"
        )
    assert create.call_count == 1


# ── the backstop, for when the sweep loses the race ────────────────────────


def test_collecting_more_than_the_invoice_owes_is_recorded_and_shouted(db, invoice):
    """The intent that succeeded microseconds before the sweep. $500 lands on
    an invoice that owes $200.

    The money MOVED, so it is recorded in full — discarding it would be
    inventing a different lie. What was missing was anyone being told.
    """
    with patch("stripe.PaymentIntent.list", return_value=_page([])):
        _mark_invoice_paid(invoice, db, external_ref="pi_stale", method="card", amount=500.0)

    live = db.execute(
        select(Payment).where(Payment.invoice_id == invoice.id, Payment.voided_at.is_(None))
    ).scalars().all()
    assert sum(float(p.amount) for p in live) == 800.00, "the money must be recorded"

    row = (
        db.query(AuditLog)
        .filter(AuditLog.action == "payment_exceeds_receivable")
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert row is not None, "an overcharge left no searchable trace"
    assert row.details["charged"] == 500.0
    assert row.details["remaining_before"] == 200.0
    assert row.details["excess"] == 300.0


def test_an_ordinary_payment_writes_no_overcharge_event(db, invoice):
    """The counterfactual. If this failed, the alert would fire on every
    payment and become wallpaper."""
    with patch("stripe.PaymentIntent.list", return_value=_page([])):
        _mark_invoice_paid(invoice, db, external_ref="pi_exact", method="card", amount=200.0)

    assert (
        db.query(AuditLog).filter(AuditLog.action == "payment_exceeds_receivable").count() == 0
    )


def test_the_webhook_passes_its_connected_account_to_the_sweep(db, invoice):
    """A platform-account scan for an intent living on a connected account
    finds nothing, which is indistinguishable from "all clear"."""
    with patch("gdx_dispatch.tasks.stale_intent_sweep.enqueue_stale_intent_sweep") as sweep:
        _mark_invoice_paid(
            invoice, db, external_ref="pi_c", method="card", amount=10.0,
            source="stripe-webhook", connected_account="acct_999",
        )
    assert sweep.call_args[1]["connected_account"] == "acct_999"


def test_the_overcharge_event_is_filed_on_the_invoice_and_names_its_source(db, invoice):
    """Money events may not be filed under one blanket system identity, and an
    operator reconstructing a bill reads the INVOICE's trail."""
    with patch("gdx_dispatch.tasks.stale_intent_sweep.enqueue_stale_intent_sweep"):
        _mark_invoice_paid(
            invoice, db, external_ref="pi_over", method="card", amount=500.0,
            source="stripe-confirm",
        )
    row = (
        db.query(AuditLog)
        .filter(AuditLog.action == "payment_exceeds_receivable")
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert row is not None
    assert row.entity_type == "invoice"
    assert str(row.entity_id) == str(invoice.id)
    assert row.user_id == "stripe-confirm", "the webhook and /confirm are different actors"

def test_two_intents_that_each_fit_but_together_overcharge(invoice):
    """The hole a per-intent test cannot see.

    Every mint is sized to the full remaining balance, so two open intents only
    exist when the balance MOVED between them — which happens whenever a
    payment is voided and the balance goes back up. Judged one at a time, an
    old $100 intent and a new $300 intent both "fit" under a $300 balance and
    both survive; confirmed together they collect $400 on $300.

    Newest-first cumulative: the $300 tab the customer is actually looking at
    survives, the stale $100 one goes.
    """
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_old", "requires_payment_method", invoice.id, amount=10000, created=1_000),
        _pi("pi_new", "requires_payment_method", invoice.id, amount=30000, created=2_000),
    ])), patch("stripe.PaymentIntent.cancel") as cancel:
        out = cancel_open_intents_for_invoice(
            invoice, why="payment_voided", remaining_cents=30000
        )

    assert [c[0][0] for c in cancel.call_args_list] == ["pi_old"], (
        "the older intent can still overcharge on top of the newer one"
    )
    assert out == [{"intent_id": "pi_old", "result": "canceled"}]


def test_the_newest_tab_is_the_one_kept(invoice):
    """Order matters: cancelling the newest and keeping a stale one would break
    the checkout the customer has open right now."""
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_newest", "requires_payment_method", invoice.id, amount=15000, created=9_999),
        _pi("pi_older", "requires_payment_method", invoice.id, amount=15000, created=1_111),
    ])), patch("stripe.PaymentIntent.cancel") as cancel:
        cancel_open_intents_for_invoice(invoice, why="x", remaining_cents=15000)

    assert [c[0][0] for c in cancel.call_args_list] == ["pi_older"]


def test_several_small_intents_are_kept_only_while_they_fit(invoice):
    """Three $60 intents against a $150 balance: two fit ($120), the third
    would push the total to $180."""
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_a", "requires_payment_method", invoice.id, amount=6000, created=3),
        _pi("pi_b", "requires_payment_method", invoice.id, amount=6000, created=2),
        _pi("pi_c", "requires_payment_method", invoice.id, amount=6000, created=1),
    ])), patch("stripe.PaymentIntent.cancel") as cancel:
        cancel_open_intents_for_invoice(invoice, why="x", remaining_cents=15000)

    assert [c[0][0] for c in cancel.call_args_list] == ["pi_c"], (
        "kept total must stay within what is owed"
    )





# ── the holes a third adversarial review found ──────────────────────────────


def test_an_in_flight_ach_inside_the_balance_is_left_alone(invoice):
    """A $200 ACH against a $200 balance is the customer paying their bill. It
    is in flight, not stale, and must not be cancelled."""
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_ach", "processing", invoice.id, amount=20000),
    ])), patch("stripe.PaymentIntent.cancel") as cancel:
        out = cancel_open_intents_for_invoice(invoice, why="x", remaining_cents=20000)
    cancel.assert_not_called()
    assert out == []


def test_an_ach_plus_a_tab_cannot_together_exceed_the_balance(invoice):
    """$500 ACH in flight and a fresh $300 tab, with `balance_due` reporting
    $300 because it cannot see the in-flight debit. Counted cumulatively,
    newest-first, the $300 tab is kept and the older $500 ACH is attempted —
    $800 must not both land on $500.
    """
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_ach", "processing", invoice.id, amount=50000, created=1),
        _pi("pi_new", "requires_payment_method", invoice.id, amount=30000, created=2),
    ])), patch("stripe.PaymentIntent.cancel") as cancel:
        cancel_open_intents_for_invoice(invoice, why="x", remaining_cents=30000)

    assert [c[0][0] for c in cancel.call_args_list] == ["pi_ach"], (
        "the two together exceed what is owed; one of them has to go"
    )



def test_a_lone_intent_under_an_untouched_balance_still_survives(invoice):
    """The counterfactual for the above: counting in-flight money must not make
    the sweep cancel everything. No ACH in flight, one exact-sized tab, spared.
    """
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_ok", "requires_payment_method", invoice.id, amount=20000),
    ])), patch("stripe.PaymentIntent.cancel") as cancel:
        cancel_open_intents_for_invoice(invoice, why="x", remaining_cents=20000)
    cancel.assert_not_called()


def test_same_second_intents_are_ordered_deterministically(invoice):
    """`created` is whole seconds and `list.sort` is stable — it does not
    reverse ties — so two intents minted in the same second would otherwise
    keep whatever order Stripe happened to return, and which of them gets
    cancelled would be unspecified. Tie-broken on id so a log can be replayed.
    """
    a = _page([
        _pi("pi_aaa", "requires_payment_method", invoice.id, amount=20000, created=777),
        _pi("pi_zzz", "requires_payment_method", invoice.id, amount=20000, created=777),
    ])
    b = _page(list(reversed(a.data)))
    picks = []
    for page in (a, b):
        with patch("stripe.PaymentIntent.list", return_value=page), \
                patch("stripe.PaymentIntent.cancel") as cancel:
            cancel_open_intents_for_invoice(invoice, why="x", remaining_cents=20000)
        picks.append([c[0][0] for c in cancel.call_args_list])

    assert picks[0] == picks[1], (
        f"the same set in a different order cancelled differently: {picks}"
    )


def test_losing_the_race_is_not_reported_as_still_collectable(invoice, caplog):
    """The browser confirmed while we were reaching for the intent. Stripe
    refuses with `payment_intent_unexpected_state`. That is NOT "still open and
    can still collect" — the money moved, and the overcharge backstop is what
    covers it. Reporting it as an open intent sends an operator looking for
    something that is not there.
    """
    err = RuntimeError(
        "You cannot cancel this PaymentIntent because it has a status of succeeded. "
        "(payment_intent_unexpected_state)"
    )
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_raced", "requires_action", invoice.id, amount=50000),
    ])), patch("stripe.PaymentIntent.cancel", side_effect=err), caplog.at_level("WARNING"):
        out = cancel_open_intents_for_invoice(invoice, why="x", remaining_cents=0)

    assert out == [{"intent_id": "pi_raced", "result": "raced"}]
    assert "stale_intent_cancel_raced" in caplog.text
    assert "can still collect" not in caplog.text


def test_a_transport_failure_is_still_reported_as_open(invoice, caplog):
    """The counterfactual: a genuine network failure DOES leave the intent
    collectable, and must keep saying so."""
    with patch("stripe.PaymentIntent.list", return_value=_page([
        _pi("pi_open", "requires_payment_method", invoice.id, amount=50000),
    ])), patch("stripe.PaymentIntent.cancel", side_effect=RuntimeError("connection reset")), \
            caplog.at_level("ERROR"):
        out = cancel_open_intents_for_invoice(invoice, why="x", remaining_cents=0)

    assert out == [{"intent_id": "pi_open", "result": "failed"}]
    assert "can still collect" in caplog.text


# ── the replay guard, rebuilt so it can actually fire ───────────────────────


def test_a_replayed_dead_intent_is_detected_by_asking_stripe(invoice, caplog):
    """Stripe's idempotency layer replays "the resulting status code and body of
    the first request" (docs.stripe.com/api/idempotent_requests, read
    2026-08-24) — the body as it was at CREATION. So a replayed create reports
    `requires_payment_method` even for an intent that is now cancelled, and the
    previous version of this guard, which read the create response's status,
    could never fire. Only a retrieve knows.
    """
    created_looks_fine = _pi("pi_dead", "requires_payment_method", invoice.id)
    actually_dead = _pi("pi_dead", "canceled", invoice.id)
    fresh = _pi("pi_fresh", "requires_payment_method", invoice.id)

    with patch("stripe.PaymentIntent.create", side_effect=[created_looks_fine, fresh]) as create, \
            patch("stripe.PaymentIntent.retrieve", return_value=actually_dead), \
            caplog.at_level("WARNING"):
        got = _create_usable_intent(
            amount=20000, currency="usd",
            metadata={"invoice_id": str(invoice.id)},
            idempotency_key="gdx-pi-x-card-20000",
        )

    assert got is fresh, "the customer was handed a pay page that cannot charge"
    assert create.call_count == 2
    assert create.call_args[1]["idempotency_key"] != "gdx-pi-x-card-20000"
    assert "intent_idempotency_replayed_dead_intent" in caplog.text


def test_a_live_intent_is_returned_without_a_second_mint(invoice):
    """The counterfactual: if this failed we would double-mint every payment."""
    made = _pi("pi_ok", "requires_payment_method", invoice.id)
    live = _pi("pi_ok", "requires_payment_method", invoice.id)
    with patch("stripe.PaymentIntent.create", return_value=made) as create, \
            patch("stripe.PaymentIntent.retrieve", return_value=live):
        got = _create_usable_intent(
            amount=20000, currency="usd", metadata={}, idempotency_key="k"
        )
    assert create.call_count == 1
    assert got is live, "the fresher object is the one to hand back"


def test_a_failed_liveness_check_does_not_break_the_checkout(invoice):
    """A create that worked and a retrieve that did not must not fail a
    customer's checkout over a verification step."""
    made = _pi("pi_ok", "requires_payment_method", invoice.id)
    with patch("stripe.PaymentIntent.create", return_value=made), \
            patch("stripe.PaymentIntent.retrieve", side_effect=RuntimeError("timeout")):
        got = _create_usable_intent(
            amount=20000, currency="usd", metadata={}, idempotency_key="k"
        )
    assert got is made


# ── cancelling a checkout is a money-object change: it leaves a trail ───────


def test_the_sweep_writes_an_audit_row_for_what_it_cancelled(db, invoice):
    """Invariant #1: every state change on a money object answers who/what/when.
    A first version logged at WARNING and stopped there, from a task that was
    holding a session the whole time."""
    from gdx_dispatch.tasks.stale_intent_sweep import sweep_stale_intents

    with patch("gdx_dispatch.tasks.stale_intent_sweep.SessionLocal") as SL, \
            patch("gdx_dispatch.core.payments.cancel_open_intents_for_invoice",
                  return_value=[{"intent_id": "pi_x", "result": "canceled"}]):
        SL.return_value.__enter__.return_value = db
        sweep_stale_intents(str(invoice.id), why="payment_recorded")

    row = (
        db.query(AuditLog)
        .filter(AuditLog.action == "stale_payment_intents_canceled")
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert row is not None, "a cancelled checkout left no trail"
    assert row.entity_type == "invoice"
    assert str(row.entity_id) == str(invoice.id)
    assert row.details["intents"] == [{"intent_id": "pi_x", "result": "canceled"}]
    assert row.details["why"] == "payment_recorded"


def test_an_empty_sweep_writes_no_audit_row(db, invoice):
    """The counterfactual. A row per ordinary sweep that found nothing would
    bury the ones that did."""
    from gdx_dispatch.tasks.stale_intent_sweep import sweep_stale_intents

    with patch("gdx_dispatch.tasks.stale_intent_sweep.SessionLocal") as SL, \
            patch("gdx_dispatch.core.payments.cancel_open_intents_for_invoice",
                  return_value=[]):
        SL.return_value.__enter__.return_value = db
        sweep_stale_intents(str(invoice.id), why="payment_recorded")

    assert db.query(AuditLog).filter(
        AuditLog.action == "stale_payment_intents_canceled"
    ).count() == 0


def test_the_task_reads_the_balance_itself(db, invoice):
    """The caller passes one bit — "was this settled" — not an amount. The
    invoice owes $200, so the sweep must be told 20000, read here and now."""
    from gdx_dispatch.tasks.stale_intent_sweep import sweep_stale_intents

    with patch("gdx_dispatch.tasks.stale_intent_sweep.SessionLocal") as SL, \
            patch("gdx_dispatch.core.payments.cancel_open_intents_for_invoice",
                  return_value=[]) as sweep:
        SL.return_value.__enter__.return_value = db
        sweep_stale_intents(str(invoice.id), why="payment_recorded")
    assert sweep.call_args[1]["remaining_cents"] == 20000


def test_settled_means_nothing_is_owed_whatever_the_row_says(db, invoice):
    """A void passes `settled=True`. The task must not re-read its way back to
    a non-zero balance."""
    from gdx_dispatch.tasks.stale_intent_sweep import sweep_stale_intents

    with patch("gdx_dispatch.tasks.stale_intent_sweep.SessionLocal") as SL, \
            patch("gdx_dispatch.core.payments.cancel_open_intents_for_invoice",
                  return_value=[]) as sweep:
        SL.return_value.__enter__.return_value = db
        sweep_stale_intents(str(invoice.id), why="invoice_voided", settled=True)
    assert sweep.call_args[1]["remaining_cents"] == 0


# ── the Connect account is resolved, not left to four call sites ────────────


def test_the_task_resolves_the_connect_account_itself(db, invoice):
    """The four OFFICE call sites (record payment, credit memo, applied credit,
    void) have no reason to know the Stripe Connect account and never passed it,
    while every Stripe-driven path does. On a Connect tenant that made the sweep
    scan the PLATFORM account and find nothing — silently no-opping on exactly
    the call site this fix exists for.

    Reading it in the task means no call site can forget.
    """
    from sqlalchemy import text as _text

    from gdx_dispatch.tasks.stale_intent_sweep import sweep_stale_intents

    db.execute(_text("CREATE TABLE IF NOT EXISTS companies (id VARCHAR(36) PRIMARY KEY, "
                     "stripe_connect_account_id VARCHAR(120))"))
    db.execute(_text("INSERT INTO companies (id, stripe_connect_account_id) VALUES (:i, :a)"),
               {"i": TENANT, "a": "acct_from_db"})
    db.commit()

    with patch("gdx_dispatch.tasks.stale_intent_sweep.SessionLocal") as SL, \
            patch("gdx_dispatch.core.payments.cancel_open_intents_for_invoice",
                  return_value=[]) as sweep:
        SL.return_value.__enter__.return_value = db
        sweep_stale_intents(str(invoice.id), why="payment_recorded")

    assert sweep.call_args[1]["connected_account"] == "acct_from_db", (
        "an intent minted on the connected account would not be seen"
    )


def test_an_explicit_account_wins_over_the_lookup(db, invoice):
    """The webhook knows the account from the event envelope — that is the
    freshest source and must not be second-guessed by a table read."""
    from sqlalchemy import text as _text

    from gdx_dispatch.tasks.stale_intent_sweep import sweep_stale_intents

    db.execute(_text("CREATE TABLE IF NOT EXISTS companies (id VARCHAR(36) PRIMARY KEY, "
                     "stripe_connect_account_id VARCHAR(120))"))
    db.execute(_text("INSERT INTO companies (id, stripe_connect_account_id) VALUES (:i, :a)"),
               {"i": TENANT, "a": "acct_stale"})
    db.commit()

    with patch("gdx_dispatch.tasks.stale_intent_sweep.SessionLocal") as SL, \
            patch("gdx_dispatch.core.payments.cancel_open_intents_for_invoice",
                  return_value=[]) as sweep:
        SL.return_value.__enter__.return_value = db
        sweep_stale_intents(str(invoice.id), why="w", connected_account="acct_from_event")

    assert sweep.call_args[1]["connected_account"] == "acct_from_event"


def test_no_companies_row_means_the_platform_account(db, invoice):
    """A single-tenant deployment with no connected account at all: the lookup
    must fall back quietly, not raise."""
    from gdx_dispatch.tasks.stale_intent_sweep import sweep_stale_intents

    with patch("gdx_dispatch.tasks.stale_intent_sweep.SessionLocal") as SL, \
            patch("gdx_dispatch.core.payments.cancel_open_intents_for_invoice",
                  return_value=[]) as sweep:
        SL.return_value.__enter__.return_value = db
        sweep_stale_intents(str(invoice.id), why="w")

    assert sweep.call_args[1]["connected_account"] == ""


def test_a_failing_overcharge_audit_does_not_lose_the_payment(db, invoice):
    """The savepoint's entire reason for existing, and it was untested.

    `log_audit_event_sync` ends in a flush. Without a SAVEPOINT, a failed flush
    poisons the session and the very next `db.commit()` — the one that saves the
    PAYMENT — goes down with it. Losing the alert is survivable; losing the
    money record is the defect class this repo ranks highest.

    Note this asserts the behaviour on SQLite, where SQLAlchemy's pysqlite
    driver has documented SAVEPOINT quirks. Prod is Postgres. What is proven
    here is that the payment survives; that is the property that matters.
    """
    with patch("gdx_dispatch.tasks.stale_intent_sweep.enqueue_stale_intent_sweep"), \
            patch("gdx_dispatch.core.audit.log_audit_event_sync",
                  side_effect=RuntimeError("audit table is on fire")):
        _mark_invoice_paid(
            invoice, db, external_ref="pi_over_fail", method="card", amount=500.0,
        )

    live = db.execute(
        select(Payment).where(
            Payment.invoice_id == invoice.id,
            Payment.reference == "pi_over_fail",
            Payment.voided_at.is_(None),
        )
    ).scalars().all()
    assert len(live) == 1, (
        "the audit write failed and took the payment with it — the money moved "
        "at Stripe and there is now no local record of it"
    )
    assert float(live[0].amount) == 500.00


