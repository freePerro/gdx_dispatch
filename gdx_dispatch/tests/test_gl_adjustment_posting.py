"""GL Phase 1 (S7) — credit memos / refunds / apply-credit on
invoice_adjustments + bug #4 (amount_paid mutation).

Plan gates: both-reasons posting (4900 vs 4910), caps (credit ≤ remaining
balance; refund ≤ net paid; apply-credit ≤ both the 2300 balance and the
remaining balance), _recalculate learns the adjustments table, invariant
holds across sequences.
"""
from __future__ import annotations

import datetime as dt
import secrets
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from gdx_dispatch.models.tenant_models import Invoice, InvoiceAdjustment, InvoiceLine, Payment
from gdx_dispatch.modules.ledger.models import GlAccount, GlJournalEntry, GlJournalLine
from gdx_dispatch.modules.ledger.rules import customer_credit_balance_cents
from gdx_dispatch.modules.ledger.service import ensure_gl_seed, transition_invoice_status
from gdx_dispatch.routers.invoices import (
    ApplyCreditIn,
    CreditMemoIn,
    PaymentCreateIn,
    RefundIn,
    apply_customer_credit,
    issue_credit_memo,
    process_refund,
    record_payment,
    void_invoice,
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


def _invoice(db, total="1000.00", status="draft", customer_id=None):
    inv = Invoice(
        id=uuid4(),
        customer_id=customer_id or uuid4(),
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


def _pay(db, inv, amount, **kw):
    return record_payment(inv.id, PaymentCreateIn(amount=amount, method="cash", **kw), _=USER, db=db)


def _credit(db, inv, amount, reason="warranty"):
    return issue_credit_memo(str(inv.id), CreditMemoIn(amount=amount, reason=reason), db=db, _=USER)


def _refund(db, inv, amount, method="check", reason="warranty"):
    return process_refund(
        str(inv.id), RefundIn(amount=amount, reason=reason, refund_method=method), db=db, _=USER
    )


def _entries(db):
    return db.scalars(select(GlJournalEntry).order_by(GlJournalEntry.entry_no)).all()


def _lines_by_code(db, entry):
    out = {}
    for line in db.scalars(select(GlJournalLine).where(GlJournalLine.entry_id == entry.id)):
        acct = db.get(GlAccount, line.account_id)
        out[acct.code] = out.get(acct.code, 0) + line.amount_cents
    return out


def _live_ar_cents(db) -> int:
    """Economic AR = the sum over ALL journal lines — reversal entries
    negate their originals, so no status filtering (a reversed original's
    lines still exist; its reversal cancels them)."""
    ar = db.scalars(select(GlAccount).where(GlAccount.role == "AR")).one()
    return sum(
        line.amount_cents
        for line in db.scalars(select(GlJournalLine).where(GlJournalLine.account_id == ar.id))
    )


# ---------------------------------------------------------------------------
# Credit memos
# ---------------------------------------------------------------------------

def test_credit_memo_reduces_balance_without_touching_amount_paid(db):
    """bug #4: the old endpoint inflated amount_paid, which recalc ignores —
    the credit evaporated on the next recalculation."""
    inv = _invoice(db, total="500.00", status="sent")
    _credit(db, inv, 200.0)
    db.refresh(inv)
    assert float(inv.balance_due) == 300.0
    assert float(inv.amount_paid) == 0.0
    row = db.scalars(select(InvoiceAdjustment)).one()
    assert row.kind == "credit_memo" and float(row.amount) == 200.0

    # the fix survives recalculation (the old bug's exact failure mode)
    from gdx_dispatch.routers.invoices import _recalculate_invoice
    _recalculate_invoice(inv, db)
    db.commit()
    assert float(inv.balance_due) == 300.0


def test_credit_memo_cap_is_remaining_balance(db):
    inv = _invoice(db, total="100.00", status="sent")
    _pay(db, inv, 60.0)
    with pytest.raises(HTTPException) as exc:
        _credit(db, inv, 50.0)  # only 40 remains
    assert exc.value.status_code == 422
    db.rollback()
    _credit(db, inv, 40.0)
    db.refresh(inv)
    assert float(inv.balance_due) == 0.0
    assert inv.status == "paid"  # fully settled via the chokepoint


def test_credit_memo_posts_reason_mapped_contra_revenue(db):
    _enable(db)
    inv = _invoice(db, total="300.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()

    _credit(db, inv, 100.0, reason="discount")   # → 4900
    _credit(db, inv, 50.0, reason="warranty")    # → 4910
    memos = [e for e in _entries(db) if e.source_type == "adjustment"]
    assert _lines_by_code(db, memos[0])["4900"] == 10_000
    assert _lines_by_code(db, memos[0])["1200"] == -10_000
    assert _lines_by_code(db, memos[1])["4910"] == 5_000
    db.refresh(inv)
    assert float(inv.balance_due) == 150.0
    assert _live_ar_cents(db) == 15_000


def test_unknown_reason_defaults_to_refunds_bucket(db):
    _enable(db)
    inv = _invoice(db, total="100.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _credit(db, inv, 10.0, reason="mystery-nonsense")
    memo = [e for e in _entries(db) if e.source_type == "adjustment"][0]
    assert "4910" in _lines_by_code(db, memo)  # conservative bucket


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

def test_refund_capped_by_net_paid_not_amount_paid(db):
    inv = _invoice(db, total="200.00", status="sent")
    inv.amount_paid = Decimal("999.00")  # deprecated garbage — must be ignored
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _refund(db, inv, 50.0)  # nothing actually paid
    assert exc.value.status_code == 422
    db.rollback()

    _pay(db, inv, 100.0)
    _refund(db, inv, 60.0)
    with pytest.raises(HTTPException) as exc:
        _refund(db, inv, 60.0)  # only 40 net remains
    assert exc.value.status_code == 422
    db.rollback()


def test_refund_posts_contra_revenue_and_cash_out(db):
    _enable(db)
    inv = _invoice(db, total="100.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _pay(db, inv, 100.0)

    _refund(db, inv, 30.0, method="check", reason="warranty")
    refunds = [e for e in _entries(db) if e.idempotency_key and ":refund:" in e.idempotency_key]
    by_code = _lines_by_code(db, refunds[0])
    assert by_code["4910"] == 3_000       # contra-revenue debit
    assert by_code["1050"] == -3_000      # check → undeposited (payment map)
    db.refresh(inv)
    assert float(inv.balance_due) == 0.0  # refunds do NOT change the balance
    assert inv.status == "paid"


def test_refund_requires_method_when_posting(db):
    _enable(db)
    inv = _invoice(db, total="100.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _pay(db, inv, 100.0)
    with pytest.raises(HTTPException) as exc:
        process_refund(str(inv.id), RefundIn(amount=10.0, reason="x"), db=db, _=USER)
    assert exc.value.status_code == 422 and "refund_method" in exc.value.detail
    db.rollback()


# ---------------------------------------------------------------------------
# Apply credit (P9)
# ---------------------------------------------------------------------------

def _seed_customer_credit(db, customer_id, invoice_total="100.00", pay=150.0):
    """Overpay an invoice to mint a 2300 balance for the customer."""
    inv = _invoice(db, total=invoice_total, customer_id=customer_id)
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _pay(db, inv, pay, allow_overpayment=True)
    return inv


def test_apply_credit_happy_path_and_dual_caps(db):
    _enable(db)
    customer_id = uuid4()
    _seed_customer_credit(db, customer_id)  # 50 credit on 2300
    assert customer_credit_balance_cents(db, COMPANY, customer_id) == 5_000

    target = _invoice(db, total="80.00", customer_id=customer_id)
    transition_invoice_status(db, target, "sent")
    db.commit()

    with pytest.raises(HTTPException) as exc:  # cap 1: credit balance
        apply_customer_credit(target.id, ApplyCreditIn(amount=60.0), db=db, _=USER)
    assert "credit balance" in exc.value.detail
    db.rollback()

    result = apply_customer_credit(target.id, ApplyCreditIn(amount=50.0), db=db, _=USER)
    assert result["balance_due"] == 30.0
    assert result["remaining_credit"] == 0.0
    assert customer_credit_balance_cents(db, COMPANY, customer_id) == 0
    applied = [e for e in _entries(db) if e.idempotency_key and ":credit_applied:" in e.idempotency_key]
    by_code = _lines_by_code(db, applied[0])
    assert by_code["2300"] == 5_000 and by_code["1200"] == -5_000

    with pytest.raises(HTTPException):  # nothing left to apply
        apply_customer_credit(target.id, ApplyCreditIn(amount=1.0), db=db, _=USER)
    db.rollback()


def test_apply_credit_capped_by_invoice_balance(db):
    _enable(db)
    customer_id = uuid4()
    _seed_customer_credit(db, customer_id, invoice_total="100.00", pay=200.0)  # 100 credit
    target = _invoice(db, total="30.00", customer_id=customer_id)
    transition_invoice_status(db, target, "sent")
    db.commit()
    with pytest.raises(HTTPException) as exc:  # cap 2: remaining balance
        apply_customer_credit(target.id, ApplyCreditIn(amount=50.0), db=db, _=USER)
    assert "remaining balance" in exc.value.detail
    db.rollback()


def test_apply_credit_requires_posting_flag(db):
    inv = _invoice(db, total="50.00", status="sent")
    with pytest.raises(HTTPException) as exc:
        apply_customer_credit(inv.id, ApplyCreditIn(amount=10.0), db=db, _=USER)
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# Void interplay + invariant
# ---------------------------------------------------------------------------

def test_invoice_void_reverses_adjustment_entries(db):
    _enable(db)
    inv = _invoice(db, total="100.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _credit(db, inv, 40.0)

    void_invoice(inv.id, _=USER, db=db)
    live = [e for e in _entries(db) if e.status == "posted" and e.reverses_entry_id is None]
    assert live == []            # P1 AND the credit memo both reversed
    assert _live_ar_cents(db) == 0


def test_invariant_across_credit_refund_sequence(db):
    """issue → pay → credit → refund: live AR always equals balance_due."""
    _enable(db)
    inv = _invoice(db, total="400.00")
    transition_invoice_status(db, inv, "sent")
    db.commit()

    def check():
        db.refresh(inv)
        assert _live_ar_cents(db) == round(float(inv.balance_due) * 100)

    check()
    _pay(db, inv, 150.0); check()
    _credit(db, inv, 100.0); check()          # AR 150 == balance 150
    _refund(db, inv, 50.0); check()           # refund: balance unchanged
    _credit(db, inv, 150.0); check()          # settle → paid, AR 0
    db.refresh(inv)
    assert inv.status == "paid"


def test_credit_then_pay_full_printed_amount_mints_customer_credit(db):
    """Audit round 3 (executed repro): credit memo first, then the customer
    pays the PRINTED total — the gate must catch it, and the opt-in must
    route the credited portion to 2300 instead of driving AR negative."""
    _enable(db)
    customer_id = uuid4()
    inv = _invoice(db, total="600.00", customer_id=customer_id)
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _credit(db, inv, 75.0)

    with pytest.raises(HTTPException) as exc:  # gate measures the REMAINING receivable
        _pay(db, inv, 600.0)
    assert exc.value.status_code == 422
    db.rollback()

    _pay(db, inv, 600.0, allow_overpayment=True)
    db.refresh(inv)
    assert float(inv.balance_due) == 0.0
    assert _live_ar_cents(db) == 0                      # never negative
    assert customer_credit_balance_cents(db, COMPANY, customer_id) == 7_500

    # replay determinism after the credit: re-posting the payment lands on
    # the live entry, no double-post
    from gdx_dispatch.modules.ledger.rules import post_payment_received
    payment = db.scalars(select(Payment).where(Payment.invoice_id == inv.id)).one()
    before = len(_entries(db))
    post_payment_received(db, payment, inv)
    db.commit()
    assert len(_entries(db)) == before


def test_overpay_void_leaves_zero_not_negative_credit(db):
    """Audit round 3 (executed repro): the 2300 balance summed reversals
    without their originals — an overpay-void produced NEGATIVE available
    credit and locked the customer out of credit they were owed."""
    from gdx_dispatch.routers.invoices import void_payment

    _enable(db)
    customer_id = uuid4()
    inv = _invoice(db, total="100.00", customer_id=customer_id)
    transition_invoice_status(db, inv, "sent")
    db.commit()
    _pay(db, inv, 150.0, allow_overpayment=True)
    assert customer_credit_balance_cents(db, COMPANY, customer_id) == 5_000

    payment = db.scalars(select(Payment).where(Payment.invoice_id == inv.id)).one()
    void_payment(inv.id, payment.id, _=USER, db=db)
    assert customer_credit_balance_cents(db, COMPANY, customer_id) == 0  # not −5000

    # a real credit on ANOTHER invoice still spendable after the void
    _seed_customer_credit(db, customer_id, invoice_total="100.00", pay=130.0)
    assert customer_credit_balance_cents(db, COMPANY, customer_id) == 3_000


def test_adjustments_rejected_on_drafts(db):
    """Audit round 3: a credit memo on a draft posts an AR credit P1 never
    debited; refunds/apply-credit are equally meaningless pre-issuance."""
    _enable(db)
    inv = _invoice(db)  # draft
    for call in (
        lambda: _credit(db, inv, 10.0),
        lambda: _refund(db, inv, 10.0),
        lambda: apply_customer_credit(inv.id, ApplyCreditIn(amount=10.0), db=db, _=USER),
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 409
        db.rollback()
    assert _entries(db) == []


def test_flag_off_adjustments_still_fix_bug4(db):
    """The bug #4 fix is deliberately flag-independent: credit memos must
    reduce the balance durably even before cutover."""
    inv = _invoice(db, total="100.00", status="sent")
    _credit(db, inv, 100.0)
    db.refresh(inv)
    assert inv.status == "paid"
    assert float(inv.balance_due) == 0.0
    assert _entries(db) == []  # no ledger writes with the flag off