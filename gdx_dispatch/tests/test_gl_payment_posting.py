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
    process_refund,
    record_payment,
    void_invoice,
    void_payment,
)
from gdx_dispatch.routers.invoices import RefundIn

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
        amount_paid=Decimal("0.00"),
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


def _pay(db, inv, amount, method="cash", allow_overpayment=False):
    return record_payment(
        inv.id,
        PaymentCreateIn(amount=amount, method=method, allow_overpayment=allow_overpayment),
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
    _pay(db, inv, 60.0)
    _pay(db, inv, 60.0, allow_overpayment=True)  # 20 over
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
    _pay(db, inv, 60.0)                                # A
    _pay(db, inv, 60.0, allow_overpayment=True)        # B: 40 AR + 20 credit
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

    _pay(db, inv, 50.0); _assert_invariant(db, inv)
    _pay(db, inv, 170.0, allow_overpayment=True); _assert_invariant(db, inv)
    first = db.scalars(
        select(Payment).where(Payment.invoice_id == inv.id).order_by(Payment.created_at)
    ).first()
    void_payment(inv.id, first.id, _=USER, db=db); _assert_invariant(db, inv)
    _pay(db, inv, 30.0); _assert_invariant(db, inv)


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
    inv.amount_paid = Decimal("100.00")
    db.commit()

    process_refund(str(inv.id), RefundIn(amount=100.0, reason="test"), db=db, _=USER)
    db.refresh(inv)
    assert inv.status != "refunded"
    assert inv.status == "paid"  # lifecycle untouched until S7 rebuilds refunds
