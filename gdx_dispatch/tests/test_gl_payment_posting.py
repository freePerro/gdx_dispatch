"""GL Phase 1 (S6) — P3/P4 payment posting, payment void, bug #1/#2 fixes.

Plan gates: partial-pay + void-ordering + overpayment tests; flag off =
identical behavior; _mark_invoice_paid records a real idempotent Payment
row through the chokepoint.
"""
from __future__ import annotations

import datetime as dt
import secrets
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from gdx_dispatch.core.payments import _mark_invoice_paid
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Payment
from gdx_dispatch.modules.ledger.models import GlAccount, GlJournalEntry, GlJournalLine
from gdx_dispatch.modules.ledger.service import ensure_gl_seed, transition_invoice_status
from gdx_dispatch.routers.invoices import (
    PaymentCreateIn,
    RefundIn,
    process_refund,
    record_payment,
    void_invoice,
    void_payment,
)

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"tenant_id": COMPANY, "sub": "tester"}


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    ensure_gl_seed(tenant_db, COMPANY)
    tenant_db.commit()
    return tenant_db


def _enable(db):
    settings = ensure_gl_seed(db, COMPANY)
    settings.ledger_posting_enabled = True
    db.commit()


def _invoice(db, total="1000.00", status="draft"):
    inv = Invoice(
        id=uuid4(),
        customer_id=uuid4(),
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status=status,
        subtotal=Decimal(total),
        tax_amount=Decimal("0.00"),
        total=Decimal(total),
        balance_due=Decimal(total),
        invoice_date=dt.date(2026, 7, 1),
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=inv.id, description="Work", quantity=1,
            unit_price=Decimal(total), line_total=Decimal(total), company_id=COMPANY,
        )
    )
    db.commit()
    db.refresh(inv)
    return inv


def _pay(db, inv, amount, method="cash", allow_overpayment=False, reference=None):
    """Record a payment through the real endpoint.

    `reference` matters for same-amount repeats: record_payment refuses two
    identical reference-less payments on one invoice inside a short window
    (that is how a replayed cash payment is caught, since cash has no natural
    dedupe key). These GL tests deliberately post two equal partials to
    exercise the AR/2300 split, so they pass a reference — which is exactly
    what the 409 tells a real operator to do.
    """
    return record_payment(
        inv.id,
        PaymentCreateIn(
            amount=amount, method=method,
            allow_overpayment=allow_overpayment, reference=reference,
        ),
        _=USER,
        db=db,
    )


def _entries(db):
    return db.scalars(select(GlJournalEntry).order_by(GlJournalEntry.entry_no)).all()


def _lines_by_code(db, entry):
    out = {}
    for line in db.scalars(select(GlJournalLine).where(GlJournalLine.entry_id == entry.id)):
        acct = db.get(GlAccount, line.account_id)
        out[acct.code] = out.get(acct.code, 0) + line.amount_cents
    return out


# ---------------------------------------------------------------------------
# flag OFF — identical behavior
# ---------------------------------------------------------------------------

def test_flag_off_payment_posts_nothing_and_overpayment_stays_permissive(db):
    inv = _invoice(db, total="100.00", status="sent")
    _pay(db, inv, 150.0)  # overpayment allowed with flag off, like today
    assert _entries(db) == []
    db.refresh(inv)
    assert inv.status == "paid"


# ---------------------------------------------------------------------------
# P3
# ---------------------------------------------------------------------------

def test_partial_payment_posts_p3(db):
    _enable(db)
    inv = _invoice(db, total="1000.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()

    _pay(db, inv, 400.0, method="cash")
    entries = _entries(db)
    assert len(entries) == 2  # P1 + P3
    by_code = _lines_by_code(db, entries[1])
    assert by_code["1050"] == 40_000     # cash → Undeposited Funds
    assert by_code["1200"] == -40_000
    db.refresh(inv)
    assert inv.status == "sent"          # partial — no flip


def test_method_map_routes_zelle_to_operating_bank(db):
    _enable(db)
    inv = _invoice(db, total="100.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _pay(db, inv, 100.0, method="Zelle")
    by_code = _lines_by_code(db, _entries(db)[1])
    assert by_code["1000"] == 10_000     # zelle → Operating Bank


def test_full_payment_on_draft_posts_p1_before_p3(db):
    """Spec §5.1/§5.3: the auto-flip posts P1 in the same transaction,
    before P3 — negative AR structurally impossible."""
    _enable(db)
    inv = _invoice(db, total="500.00")  # stays draft
    _pay(db, inv, 500.0)
    entries = _entries(db)
    assert len(entries) == 2
    assert entries[0].idempotency_key.startswith(f"invoice:{inv.id}:issued:")
    assert entries[1].idempotency_key.startswith("payment:")
    db.refresh(inv)
    assert inv.status == "paid"


def test_overpayment_rejected_without_opt_in(db):
    _enable(db)
    inv = _invoice(db, total="100.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _pay(db, inv, 150.0)
    assert exc.value.status_code == 422
    assert "allow_overpayment" in exc.value.detail
    db.rollback()
    assert len(_entries(db)) == 1  # only P1


def test_overpayment_opt_in_credits_2300(db):
    _enable(db)
    inv = _invoice(db, total="100.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _pay(db, inv, 150.0, allow_overpayment=True)
    by_code = _lines_by_code(db, _entries(db)[1])
    assert by_code["1050"] == 15_000
    assert by_code["1200"] == -10_000    # AR only up to the invoice
    assert by_code["2300"] == -5_000     # excess → customer credit


def test_two_partials_split_ar_correctly(db):
    _enable(db)
    inv = _invoice(db, total="100.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _pay(db, inv, 60.0, reference="partial-1")
    _pay(db, inv, 60.0, allow_overpayment=True, reference="partial-2")  # 20 over
    p3s = [e for e in _entries(db) if e.idempotency_key.startswith("payment:")]
    second = _lines_by_code(db, p3s[1])
    assert second["1200"] == -4_000
    assert second["2300"] == -2_000


# ---------------------------------------------------------------------------
# P4 — payment void
# ---------------------------------------------------------------------------

def test_void_payment_reverses_p3_and_reopens_invoice(db):
    _enable(db)
    inv = _invoice(db, total="100.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _pay(db, inv, 100.0)
    db.refresh(inv)
    assert inv.status == "paid"
    payment = db.scalars(select(Payment).where(Payment.invoice_id == inv.id)).one()

    void_payment(inv.id, payment.id, _=USER, db=db)
    db.refresh(inv)
    db.refresh(payment)
    assert payment.voided_at is not None
    assert inv.status == "sent"          # reopened
    assert float(inv.balance_due) == 100.0
    assert inv.paid_at is None
    p3 = [e for e in _entries(db) if e.idempotency_key.startswith("payment:")]
    assert p3[0].status == "reversed"


def test_void_payment_is_idempotent(db):
    _enable(db)
    inv = _invoice(db, total="100.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _pay(db, inv, 100.0)
    payment = db.scalars(select(Payment).where(Payment.invoice_id == inv.id)).one()
    void_payment(inv.id, payment.id, _=USER, db=db)
    void_payment(inv.id, payment.id, _=USER, db=db)  # no error, no double reversal
    reversals = [e for e in _entries(db) if e.reverses_entry_id is not None]
    assert len(reversals) == 1


def test_invoice_void_actionable_after_payment_void(db):
    """S5's dead-end resolved: void the payment, then the invoice."""
    _enable(db)
    inv = _invoice(db, total="100.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _pay(db, inv, 100.0)
    payment = db.scalars(select(Payment).where(Payment.invoice_id == inv.id)).one()

    with pytest.raises(HTTPException):
        void_invoice(inv.id, _=USER, db=db)   # blocked while payment live
    db.rollback()
    void_payment(inv.id, payment.id, _=USER, db=db)
    result = void_invoice(inv.id, _=USER, db=db)
    assert result["status"] == "void"
    # P1 reversed + P3 reversed → net zero ledger
    live = [e for e in _entries(db) if e.status == "posted" and e.reverses_entry_id is None]
    assert live == []


def test_void_payment_wrong_invoice_404(db):
    _enable(db)
    inv1, inv2 = _invoice(db), _invoice(db)
    _pay(db, inv1, 10.0)
    payment = db.scalars(select(Payment).where(Payment.invoice_id == inv1.id)).one()
    with pytest.raises(HTTPException) as exc:
        void_payment(inv2.id, payment.id, _=USER, db=db)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Audit round 2 — the invoice-level GL invariant + replay determinism
# ---------------------------------------------------------------------------

def _live_ar_cents(db, inv) -> int:
    """SUM of live AR lines across the invoice's P1 + all its payments' P3s.
    THE invariant: this must equal balance_due for an issued, non-void
    invoice after ANY event sequence."""
    ar = db.scalars(select(GlAccount).where(GlAccount.role == "AR")).one()
    total = 0
    for e in _entries(db):
        if e.status != "posted" or e.reverses_entry_id is not None:
            continue
        owned = e.idempotency_key.startswith(f"invoice:{inv.id}:") or (
            e.source_type == "payment"
        )
        if not owned:
            continue
        for line in db.scalars(select(GlJournalLine).where(GlJournalLine.entry_id == e.id)):
            if line.account_id == ar.id:
                total += line.amount_cents
    return total


def _assert_invariant(db, inv):
    db.refresh(inv)
    assert _live_ar_cents(db, inv) == round(float(inv.balance_due) * 100), (
        f"GL AR diverged from balance_due: {_live_ar_cents(db, inv)} vs {inv.balance_due}"
    )


def test_void_resettles_later_payment_splits(db):
    """Audit round 2 (executed repro): voiding payment A left payment B's
    AR/2300 split stale — GL AR diverged from balance_due and replays
    double-posted. The void now reverse+reposts every later payment."""
    from gdx_dispatch.modules.ledger.rules import post_payment_received

    _enable(db)
    inv = _invoice(db, total="100.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _pay(db, inv, 60.0, reference="A")                                # A
    _pay(db, inv, 60.0, allow_overpayment=True, reference="B")        # B: 40 AR + 20 credit
    payment_a = db.scalars(
        select(Payment).where(Payment.invoice_id == inv.id).order_by(Payment.created_at)
    ).first()

    void_payment(inv.id, payment_a.id, _=USER, db=db)
    _assert_invariant(db, inv)                         # AR == balance_due again

    # B's live entry now reflects the post-void split: full 60 to AR
    payment_b = db.scalars(
        select(Payment).where(Payment.invoice_id == inv.id, Payment.voided_at.is_(None))
    ).one()
    live_b = [
        e for e in _entries(db)
        if e.source_id == str(payment_b.id) and e.status == "posted" and e.reverses_entry_id is None
    ]
    assert len(live_b) == 1
    by_code = _lines_by_code(db, live_b[0])
    assert by_code["1200"] == -6_000 and "2300" not in by_code

    # replay determinism restored: re-posting B lands on the live entry
    replay = post_payment_received(db, payment_b, inv)
    db.commit()
    assert replay.id == live_b[0].id
    live_count = len([e for e in _entries(db) if e.status == "posted" and e.reverses_entry_id is None and e.source_type == "payment"])
    assert live_count == 1


def test_plain_replay_of_payment_is_idempotent(db):
    from gdx_dispatch.modules.ledger.rules import post_payment_received

    _enable(db)
    inv = _invoice(db, total="100.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _pay(db, inv, 100.0)
    payment = db.scalars(select(Payment).where(Payment.invoice_id == inv.id)).one()
    first = [e for e in _entries(db) if e.source_type == "payment"][0]
    assert post_payment_received(db, payment, inv).id == first.id
    db.commit()
    assert len([e for e in _entries(db) if e.source_type == "payment"]) == 1


def test_invariant_holds_across_event_sequence(db):
    """pay → pay-over → void → pay again: AR always equals balance_due."""
    _enable(db)
    inv = _invoice(db, total="200.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _assert_invariant(db, inv)

    _pay(db, inv, 50.0)
    _assert_invariant(db, inv)
    _pay(db, inv, 170.0, allow_overpayment=True)
    _assert_invariant(db, inv)
    first = db.scalars(
        select(Payment).where(Payment.invoice_id == inv.id).order_by(Payment.created_at)
    ).first()
    void_payment(inv.id, first.id, _=USER, db=db)
    _assert_invariant(db, inv)
    _pay(db, inv, 30.0)
    _assert_invariant(db, inv)


# ---------------------------------------------------------------------------
# bug #1 — _mark_invoice_paid records a real Payment row
# ---------------------------------------------------------------------------

def test_mark_invoice_paid_creates_idempotent_payment_row(db):
    _enable(db)
    inv = _invoice(db, total="250.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()

    _mark_invoice_paid(inv, db, external_ref="pi_test_123", method="card")
    _mark_invoice_paid(inv, db, external_ref="pi_test_123", method="card")  # webhook replay

    payments = db.scalars(select(Payment).where(Payment.invoice_id == inv.id)).all()
    assert len(payments) == 1
    assert float(payments[0].amount) == 250.0
    assert payments[0].reference == "pi_test_123"
    db.refresh(inv)
    assert inv.status == "paid"
    p3 = [e for e in _entries(db) if e.idempotency_key.startswith("payment:")]
    assert len(p3) == 1
    assert _lines_by_code(db, p3[0])["1050"] == 25_000  # card → Undeposited


def test_mark_invoice_paid_flag_off_still_records_payment(db):
    inv = _invoice(db, total="80.00", status="sent")
    _mark_invoice_paid(inv, db, external_ref="pi_off_1")
    payments = db.scalars(select(Payment).where(Payment.invoice_id == inv.id)).all()
    assert len(payments) == 1
    db.refresh(inv)
    assert inv.status == "paid"
    assert _entries(db) == []


# ---------------------------------------------------------------------------
# bug #2 — /refund no longer writes an enum-invalid status
# ---------------------------------------------------------------------------

def test_refund_does_not_write_invalid_status(db):
    inv = _invoice(db, total="100.00", status="sent")
    _pay(db, inv, 100.0)
    db.refresh(inv)
    # (Formerly also set the deprecated `amount_paid` cache here; the column is
    # dropped — migration 073 — and the assignment had become a silent no-op.)

    process_refund(str(inv.id), RefundIn(amount=100.0, reason="test"), db=db, _=USER)
    db.refresh(inv)
    assert inv.status != "refunded"
    assert inv.status == "paid"  # lifecycle untouched until S7 rebuilds refunds


# ---------------------------------------------------------------------------
# GDXA-45 — the Stripe webhook's void has to take the ledger with it
# ---------------------------------------------------------------------------
#
# P3 is posted at the RECORDING sites, not at the invoice-status chokepoint,
# so nothing in `transition_invoice_status` un-posts it: `_POSTING_RULES`
# registers no ("paid","sent") or ("sent","paid") rule, on purpose. Three
# places moved `Payment.voided_at` and only the office one resettled. A full
# Stripe refund therefore re-opened the invoice for $500 while the books kept
# $500 of Undeposited Funds that had gone back to the cardholder and kept AR
# written down to zero — and no operator action repaired it: the office refund
# endpoint 422s ("exceeds net amount paid"), and voiding by hand hits
# `void_payment`'s idempotent early return.
#
# `test_stale_failure_events.py` already drives both of these events through
# `handle_payment_webhook` and asserts the payment rows. It runs with no GL
# settings row, so posting is off and the books are never in question. These
# are the same two events with posting ON, asserting the trial balance.

def _tb(db) -> dict[str, int]:
    """The books as the repo's own reporter reads them: code → signed cents."""
    from gdx_dispatch.modules.ledger.reports import trial_balance

    report = trial_balance(db, COMPANY, as_of=dt.date(2099, 12, 31))
    return {
        row["code"]: row["debit_cents"] - row["credit_cents"]
        for row in report["rows"]
    }


def _webhook_paid_invoice(db, total="500.00", reference="pi_gdxa45", when=None):
    """A sent invoice paid in full by card, through the real record path.

    ``when`` dates the payment — and therefore its P3 — so a period lock can
    be placed over the original entry while today stays open.
    """
    inv = _invoice(db, total=total)
    transition_invoice_status(db, inv, "sent")
    db.commit()
    record_payment(
        inv.id,
        PaymentCreateIn(
            amount=float(total), method="card", reference=reference,
            **({"date": when} if when is not None else {}),
        ),
        _=USER,
        db=db,
    )
    db.refresh(inv)
    assert inv.status == "paid" and float(inv.balance_due) == 0.0
    return inv


def _charge_refunded(reference, cents):
    return {
        "type": "charge.refunded",
        "data": {
            "object": {
                "id": "ch_gdxa45", "payment_intent": reference,
                "amount": cents, "amount_refunded": cents, "refunded": True,
            }
        },
    }


def _dispute(kind, reference, cents):
    return {
        "type": f"charge.dispute.funds_{kind}",
        "data": {"object": {"id": "dp_gdxa45", "payment_intent": reference, "amount": cents}},
    }


def test_webhook_full_refund_reverses_the_payments_ledger_entry(db):
    """Direction one, the entry as filed: `charge.refunded` (full).

    Before the fix the invoice re-opened to $500 and the trial balance did not
    move — 1050 Undeposited Funds still held cash that was back on the
    cardholder's card, and AR still read zero against $500 owing.
    """
    from gdx_dispatch.core.payments import handle_payment_webhook

    _enable(db)
    inv = _webhook_paid_invoice(db)
    assert _tb(db) == {"1050": 50_000, "4000": -50_000}
    _assert_invariant(db, inv)                      # AR 0 == balance_due 0

    out = handle_payment_webhook(_charge_refunded("pi_gdxa45", 50_000), db)

    assert out["status"] == "reversed", out
    db.refresh(inv)
    assert inv.status == "sent" and float(inv.balance_due) == 500.0
    # The cash leaves 1050 and AR comes back — the books now agree with the
    # invoice, which is the whole point.
    assert _tb(db) == {"1200": 50_000, "4000": -50_000}
    _assert_invariant(db, inv)


def test_webhook_dispute_withdrawal_reverses_the_payments_ledger_entry(db):
    """Same class, the other event that reaches `_reverse_recorded_payment`:
    a chargeback. Money genuinely left; the books have to say so."""
    from gdx_dispatch.core.payments import handle_payment_webhook

    _enable(db)
    inv = _webhook_paid_invoice(db, reference="pi_gdxa45_cb")

    handle_payment_webhook(_dispute("withdrawn", "pi_gdxa45_cb", 50_000), db)

    db.refresh(inv)
    assert inv.status == "sent" and float(inv.balance_due) == 500.0
    assert _tb(db) == {"1200": 50_000, "4000": -50_000}
    _assert_invariant(db, inv)


def test_webhook_won_dispute_reposts_the_payments_ledger_entry(db):
    """Direction two, and NOT optional.

    With the reversal side resettling and the mirror left alone, winning a
    dispute restores the cash at Stripe and flips the invoice back to
    paid/$0.00 while the books stay at AR $500 / cash $0 — a latent
    overstatement traded for a live understatement. The chokepoint does not
    cover this either: there is no ("sent","paid") rule.
    """
    from gdx_dispatch.core.payments import handle_payment_webhook

    _enable(db)
    inv = _webhook_paid_invoice(db, reference="pi_gdxa45_won")
    handle_payment_webhook(_dispute("withdrawn", "pi_gdxa45_won", 50_000), db)
    assert _tb(db) == {"1200": 50_000, "4000": -50_000}, "setup: the withdrawal unwound"

    out = handle_payment_webhook(_dispute("reinstated", "pi_gdxa45_won", 50_000), db)

    assert out["status"] == "reinstated", out
    db.refresh(inv)
    assert inv.status == "paid" and float(inv.balance_due) == 0.0
    assert _tb(db) == {"1050": 50_000, "4000": -50_000}
    _assert_invariant(db, inv)


def test_webhook_refund_reversal_carries_every_leg_the_entry_has(db):
    """The reversal mirrors the ENTRY, not a hardcoded pair of legs.

    `reverse_entry` negates whatever lines the original carries, so a P3 that
    grows a leg (the card-surcharge work adds 4950 Card Surcharge Income to
    this same entry) unwinds completely without this code knowing about it.
    Asserted on the live entry rather than assumed.
    """
    from gdx_dispatch.core.payments import handle_payment_webhook

    _enable(db)
    _webhook_paid_invoice(db, reference="pi_gdxa45_legs")
    p3 = [e for e in _entries(db) if e.source_type == "payment"][0]
    original = _lines_by_code(db, p3)

    handle_payment_webhook(_charge_refunded("pi_gdxa45_legs", 50_000), db)

    reversal = [e for e in _entries(db) if e.reverses_entry_id == p3.id]
    assert len(reversal) == 1, "the P3 was not reversed"
    assert _lines_by_code(db, reversal[0]) == {k: -v for k, v in original.items()}
    db.refresh(p3)
    assert p3.status == "reversed"


def test_webhook_reversal_unwinds_into_the_open_period_when_the_original_is_locked(db):
    """The refusal path, settled.

    The office turns `PeriodLockedError` into a 409 a human answers. The
    webhook has no human and `handle_payment_webhook` raises to a 500 so
    Stripe retries — an unguarded resettle there would leave the void
    permanently uncommitted while Stripe redelivered for days and then gave
    up, losing the event. So it retries the un-posting in the current open
    period, which is `reverse_entry`'s own documented answer for unwinding a
    closed-period entry and is preferable to asserting an `accounting.close`
    override a webhook has no standing to make.
    """
    from gdx_dispatch.core.payments import handle_payment_webhook
    from gdx_dispatch.modules.ledger.models import GlPeriodLock

    _enable(db)
    inv = _webhook_paid_invoice(
        db, reference="pi_gdxa45_locked", when=dt.date(2026, 7, 5)
    )
    p3 = [e for e in _entries(db) if e.source_type == "payment"][0]
    assert p3.effective_at == dt.date(2026, 7, 5)
    # Lock through the day the payment posted: reversing at the original date
    # is now refused, but today is still open.
    db.add(GlPeriodLock(company_id=COMPANY, lock_date=p3.effective_at))
    db.commit()

    out = handle_payment_webhook(_charge_refunded("pi_gdxa45_locked", 50_000), db)

    assert out["status"] == "reversed", out
    db.refresh(inv)
    assert inv.status == "sent" and float(inv.balance_due) == 500.0, (
        "the void must commit — a 500-and-retry loop that never lands it is "
        "worse than the divergence being fixed"
    )
    reversal = [e for e in _entries(db) if e.reverses_entry_id == p3.id]
    assert len(reversal) == 1
    assert reversal[0].effective_at > p3.effective_at, (
        "the reversal was dated into the locked period"
    )
    assert _tb(db) == {"1200": 50_000, "4000": -50_000}
    _assert_invariant(db, inv)


def test_webhook_void_commits_loudly_when_even_the_open_period_is_locked(db):
    """Rung three. A future-dated lock refuses the reversal at every date.

    All-or-nothing on purpose: a half-applied resettle leaves the books
    internally inconsistent, which is worse than stale. The void still lands —
    the money fact is real — and the gap is named on the audit trail so it is
    findable, which the silent divergence this fixes was not.
    """
    from gdx_dispatch.core.audit import AuditLog
    from gdx_dispatch.core.payments import handle_payment_webhook
    from gdx_dispatch.modules.ledger.models import GlPeriodLock

    _enable(db)
    inv = _webhook_paid_invoice(db, reference="pi_gdxa45_hardlock")
    db.add(GlPeriodLock(company_id=COMPANY, lock_date=dt.date(2099, 12, 31)))
    db.commit()

    out = handle_payment_webhook(_charge_refunded("pi_gdxa45_hardlock", 50_000), db)

    assert out["status"] == "reversed", out
    db.refresh(inv)
    assert inv.status == "sent" and float(inv.balance_due) == 500.0
    assert _tb(db) == {"1050": 50_000, "4000": -50_000}, "ledger deliberately untouched"
    deferred = db.scalars(
        select(AuditLog).where(AuditLog.action == "payment_void_ledger_deferred")
    ).all()
    assert len(deferred) == 1, "the gap was left silent"
    assert deferred[0].details["why"] == "period locked"


def test_office_void_still_409s_on_a_locked_period(db):
    """The webhook's ladder must NOT leak into the office path: a person is
    reading that response and gets to decide."""
    from gdx_dispatch.modules.ledger.models import GlPeriodLock

    _enable(db)
    inv = _webhook_paid_invoice(
        db, reference="pi_gdxa45_office", when=dt.date(2026, 7, 5)
    )
    p3 = [e for e in _entries(db) if e.source_type == "payment"][0]
    db.add(GlPeriodLock(company_id=COMPANY, lock_date=p3.effective_at))
    db.commit()
    payment = db.scalars(select(Payment).where(Payment.invoice_id == inv.id)).one()

    with pytest.raises(HTTPException) as exc:
        void_payment(inv.id, payment.id, _=USER, db=db)
    assert exc.value.status_code == 409
    assert "locked accounting period" in exc.value.detail


def test_webhook_ach_return_reverses_the_payments_ledger_entry(db):
    """The fourth door into the same function, checked rather than assumed.

    Five webhook events reverse a payment and every one of them funnels
    through `_reverse_recorded_payment`: `charge.refunded` (full),
    `charge.dispute.created` (escalated), `charge.dispute.funds_withdrawn`,
    `charge.failed`, and this one — `payment_intent.payment_failed`, which is
    how a returned ACH debit (R01 insufficient funds, R10 unauthorized)
    arrives days after the money looked settled. Putting the ledger inside the
    shared helper is what makes "they all inherit it" true instead of hopeful.

    The supersession guard cannot reach Stripe here, so it logs
    `failure_event_unverified` and reverses — its documented fallback, and the
    right one: a missed reversal is far worse than one that has to be undone.
    """
    from gdx_dispatch.core.payments import handle_payment_webhook

    _enable(db)
    inv = _webhook_paid_invoice(db, reference="pi_gdxa45_ach")

    handle_payment_webhook(
        {
            "type": "payment_intent.payment_failed",
            "data": {"object": {
                "id": "pi_gdxa45_ach", "latest_charge": "ch_ach",
                "last_payment_error": {"message": "R01 insufficient funds"},
                "metadata": {"invoice_id": str(inv.id)},
            }},
        },
        db,
    )

    db.refresh(inv)
    assert inv.status == "sent" and float(inv.balance_due) == 500.0
    assert _tb(db) == {"1200": 50_000, "4000": -50_000}
    _assert_invariant(db, inv)


def test_webhook_refund_reverses_the_card_surcharge_leg_too(db):
    """The 4950 leg, named because it is the one under an open ruling.

    The card-surcharge work (2026-09-16) puts **4950 Card Surcharge Income**
    on this same P3: the bank receives amount + fee, AR is relieved by the
    amount only, and the fee is income. With posting on, a refund that left
    the entry alone overstated cash, AR *and* 4950.

    This code does not enumerate legs — `reverse_entry` negates whatever the
    entry carries — so the fix is leg-agnostic by construction. Asserted here
    anyway, because "by construction" is a claim and this is a money surface.

    WHETHER a refunded surcharge should come all the way back off 4950, or be
    retained as earned (acquirers commonly keep the fee on a chargeback), is a
    money-treatment decision for Doug and is NOT settled here. Full reversal
    is the conservative default: the entry unwinds exactly as it was posted.
    """
    from gdx_dispatch.core.payments import handle_payment_webhook
    from gdx_dispatch.modules.ledger.rules import post_payment_received

    _enable(db)
    inv = _invoice(db, total="500.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    payment = Payment(
        invoice_id=inv.id, amount=Decimal("500.00"), method="card",
        payment_date=dt.date(2026, 7, 5), reference="pi_gdxa45_fee",
        surcharge_amount=Decimal("15.00"), company_id=COMPANY,
    )
    db.add(payment)
    db.flush()
    p3 = post_payment_received(db, payment, inv)
    db.commit()
    assert _lines_by_code(db, p3)["4950"] == -1_500, "setup: no surcharge leg"
    # AR nets to zero (P1 +500 / P3 −500); the bank holds the fee on top.
    assert _tb(db) == {"1050": 51_500, "4000": -50_000, "4950": -1_500}

    # Stripe's amounts are the CHARGE's, fee included: $515 taken, $515 back.
    # (A refund of the $500 base alone is a PARTIAL and never reaches here.)
    handle_payment_webhook(_charge_refunded("pi_gdxa45_fee", 51_500), db)

    # Every leg unwinds — cash, AR and the fee income — leaving only the
    # issuance entry standing.
    assert _tb(db) == {"1200": 50_000, "4000": -50_000}
    db.refresh(inv)
    assert inv.status == "sent" and float(inv.balance_due) == 500.0


# ---------------------------------------------------------------------------
# GDXA-45, adversarial review — the locked-period fallback must be PER WRITE
# ---------------------------------------------------------------------------
#
# Every other money assertion in this file reads the trial balance as of
# 2099-12-31, where a mis-dated reversal and the entry it cancels net out. A
# suite that only ever asks "as of the end of time" is structurally incapable
# of seeing a period-dated error — which is exactly the class of error a
# locked-period fallback can introduce. These two ask as of a date INSIDE the
# window, and the first one fails on the review's first draft of the fix.

def _tb_as_of(db, as_of) -> dict[str, int]:
    from gdx_dispatch.modules.ledger.reports import trial_balance

    return {
        row["code"]: row["debit_cents"] - row["credit_cents"]
        for row in trial_balance(db, COMPANY, as_of=as_of)["rows"]
    }


def test_locked_period_fallback_does_not_re_date_unrefused_reversals(db):
    """The review's executed falsifier, frozen.

    Voiding payment A resettles payment B too — B's AR/2300 split changed. B's
    own date is open, so B's reversal and its replacement must BOTH stay on
    B's date. A first draft handed the fallback date to every `reverse_entry`
    in the loop, which stranded B's reversal in September while its
    replacement sat in August: a trial balance struck mid-August then
    double-counted B and read $180 of cash and −$60 of AR on a $100 invoice.
    """
    from gdx_dispatch.core.payments import handle_payment_webhook
    from gdx_dispatch.modules.ledger.models import GlPeriodLock

    _enable(db)
    inv = _invoice(db, total="100.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    record_payment(  # A — inside what becomes the locked period
        inv.id,
        PaymentCreateIn(amount=60.0, method="card", reference="pi_A",
                        date=dt.date(2026, 7, 5)),
        _=USER, db=db,
    )
    record_payment(  # B — after the lock; 40 to AR, 20 to customer credit
        inv.id,
        PaymentCreateIn(amount=60.0, method="card", reference="pi_B",
                        allow_overpayment=True, date=dt.date(2026, 8, 1)),
        _=USER, db=db,
    )
    db.add(GlPeriodLock(company_id=COMPANY, lock_date=dt.date(2026, 7, 31)))
    db.commit()
    assert _tb_as_of(db, dt.date(2026, 8, 15))["1050"] == 12_000  # A + B

    handle_payment_webhook(_charge_refunded("pi_A", 6_000), db)

    assert _tb_as_of(db, dt.date(2026, 8, 15)) == {
        # THE assertion. B's reversal and its replacement both stay on B's own
        # date, so they net and August still sees B's cash exactly once. The
        # first draft re-dated the reversal to today and left the replacement
        # in August: 18_000 here, B counted twice.
        "1050": 12_000,
        # A's reversal is the one write the lock genuinely refused, so it sits
        # in September and is correctly absent from August — which is why AR
        # reads negative mid-period. That is what a closed period costs, and
        # the alternative (posting into July) is the thing the lock forbids.
        "1200": -2_000,
        "4000": -10_000,
        # B's $20 customer credit is gone: with A voided, all of B applies to
        # AR. A real economic change in August, correctly dated in August.
    }
    assert _tb_as_of(db, dt.date(2099, 12, 31)) == {
        "1050": 6_000, "1200": 4_000, "4000": -10_000,
    }
    _assert_invariant(db, inv)


def test_won_dispute_reposts_even_when_the_payments_period_has_closed(db):
    """The mirror's own locked-period path, which has no reversal to re-date.

    `funds_reinstated` POSTS the payment back. If the month it belongs to has
    since closed, the only write on the path is that repost — so a fallback
    that only re-dates reversals leaves this direction with no ledger effect
    at all, and the invoice returns to paid/$0.00 while the books still read
    AR. Executed by an adversarial review against the first draft.
    """
    from gdx_dispatch.core.payments import handle_payment_webhook
    from gdx_dispatch.modules.ledger.models import GlPeriodLock

    _enable(db)
    inv = _webhook_paid_invoice(
        db, reference="pi_gdxa45_wonlate", when=dt.date(2026, 7, 5)
    )
    handle_payment_webhook(_dispute("withdrawn", "pi_gdxa45_wonlate", 50_000), db)
    assert _tb(db) == {"1200": 50_000, "4000": -50_000}, "setup: the chargeback unwound"
    db.add(GlPeriodLock(company_id=COMPANY, lock_date=dt.date(2026, 7, 31)))
    db.commit()

    out = handle_payment_webhook(_dispute("reinstated", "pi_gdxa45_wonlate", 50_000), db)

    assert out["status"] == "reinstated", out
    db.refresh(inv)
    assert inv.status == "paid" and float(inv.balance_due) == 0.0
    assert _tb(db) == {"1050": 50_000, "4000": -50_000}, (
        "the invoice went back to paid while the books stayed at AR"
    )
    _assert_invariant(db, inv)
