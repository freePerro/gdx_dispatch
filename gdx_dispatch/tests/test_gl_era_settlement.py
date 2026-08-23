"""Late-learned era settlements (2026-08-17 — the 41 locked replay events).

An ERA invoice's payment DATED before cutover but RECORDED after it (the
August QB paid-status backfill) is an era FACT learned late: the P8 anchor
snapshot is overstated by exactly that settlement, and the cash already
sits inside the statement-derived opening bank balances. So it must post
as an anchor CORRECTION — debit 3950 Opening Balance Equity / credit 1200
AR at the cutover date — never a P3 (double-counted cash + era-lock crash),
and never rot forever in the replay's locked list.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Payment
from gdx_dispatch.modules.ledger.models import (
    ROLE_AR,
    ROLE_OPENING_EQUITY,
    GlAccount,
    GlJournalEntry,
    GlJournalLine,
    GlPeriodLock,
)
from gdx_dispatch.modules.ledger.rules import (
    EVENT_ERA_SETTLEMENT,
    invoice_ar_balance_cents,
    post_opening_balance,
    post_payment_received,
    resettle_invoice_payments,
)
from gdx_dispatch.modules.ledger.service import ensure_gl_seed

COMPANY = "11111111-1111-1111-1111-111111111111"
CUTOVER = date(2026, 7, 1)
POST = datetime(2026, 8, 1, 12, 0)


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    settings = ensure_gl_seed(tenant_db, COMPANY)
    settings.cutover_month = CUTOVER
    tenant_db.add(GlPeriodLock(company_id=COMPANY, lock_date=CUTOVER - __import__("datetime").timedelta(days=1)))
    tenant_db.commit()
    return tenant_db


def _enable(db):
    """Flag flips AFTER fixtures exist — the chokepoint guard forbids
    invoices born 'sent' under live posting (correctly)."""
    settings = ensure_gl_seed(db, COMPANY)
    settings.ledger_posting_enabled = True
    db.commit()


def _era_invoice(db, total="1000.00"):
    inv = Invoice(
        id=uuid4(), customer_id=uuid4(),
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status="sent", subtotal=Decimal(total), tax_amount=Decimal("0.00"),
        total=Decimal(total), balance_due=Decimal(total),
        invoice_date=date(2026, 5, 12),
        public_token=secrets.token_urlsafe(48)[:64], company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(invoice_id=inv.id, description="Door",
                       quantity=1, unit_price=Decimal(total),
                       line_total=Decimal(total), company_id=COMPANY))
    db.flush()
    return inv


def _late_payment(db, invoice, *, amount, paid_on=date(2026, 6, 20)):
    """DATED pre-cutover, RECORDED post-cutover — the late-learned shape."""
    p = Payment(id=uuid4(), invoice_id=invoice.id, amount=Decimal(amount),
                method="check", payment_date=paid_on, company_id=COMPANY)
    db.add(p)
    db.flush()
    p.created_at = POST
    db.flush()
    return p


def _era_entries(db, payment):
    return db.scalars(select(GlJournalEntry).where(
        GlJournalEntry.source_type == "payment",
        GlJournalEntry.source_id == str(payment.id),
        GlJournalEntry.status == "posted",
        GlJournalEntry.idempotency_key.like(f"payment:{payment.id}:{EVENT_ERA_SETTLEMENT}:%"),
    )).all()


def test_late_era_payment_settles_anchor_not_cash(db):
    inv = _era_invoice(db)
    _enable(db)
    post_opening_balance(db, inv, actor="t")  # P8 anchors full $1000
    db.commit()
    assert invoice_ar_balance_cents(db, inv) == 100000

    p = _late_payment(db, inv, amount="1000.00")
    resettle_invoice_payments(db, inv, actor="t")
    db.commit()

    entries = _era_entries(db, p)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.effective_at == CUTOVER  # first open day, era lock respected
    lines = db.scalars(select(GlJournalLine).where(GlJournalLine.entry_id == entry.id)).all()
    by_role = {db.get(GlAccount, line.account_id).role: int(line.amount_cents) for line in lines}
    assert by_role[ROLE_OPENING_EQUITY] == 100000   # equity debit (anchor correction)
    assert by_role[ROLE_AR] == -100000              # AR credit — NOT cash
    assert invoice_ar_balance_cents(db, inv) == 0   # ledger AR now matches truth


def test_allocation_caps_at_anchor_and_is_idempotent(db):
    inv = _era_invoice(db, total="100.00")
    _enable(db)
    post_opening_balance(db, inv, actor="t")
    p1 = _late_payment(db, inv, amount="80.00", paid_on=date(2026, 6, 10))
    p2 = _late_payment(db, inv, amount="50.00", paid_on=date(2026, 6, 20))
    resettle_invoice_payments(db, inv, actor="t")
    db.commit()

    def cents(p):
        e = _era_entries(db, p)
        if not e:
            return 0
        return max(int(line.amount_cents) for line in db.scalars(
            select(GlJournalLine).where(GlJournalLine.entry_id == e[0].id)).all())

    assert cents(p1) == 8000
    assert cents(p2) == 2000  # capped: pool exhausted at the anchor amount
    assert invoice_ar_balance_cents(db, inv) == 0

    before = db.scalar(select(__import__("sqlalchemy").func.count(GlJournalEntry.id)))
    resettle_invoice_payments(db, inv, actor="t")  # replay — same keys
    db.commit()
    after = db.scalar(select(__import__("sqlalchemy").func.count(GlJournalEntry.id)))
    assert before == after


def test_void_reverses_the_settlement(db):
    inv = _era_invoice(db, total="500.00")
    _enable(db)
    post_opening_balance(db, inv, actor="t")
    p = _late_payment(db, inv, amount="500.00")
    resettle_invoice_payments(db, inv, actor="t")
    db.commit()
    assert invoice_ar_balance_cents(db, inv) == 0

    p.voided_at = datetime(2026, 8, 10, 9, 0)
    db.flush()
    resettle_invoice_payments(db, inv, actor="t")
    db.commit()
    assert invoice_ar_balance_cents(db, inv) == 50000  # receivable restored
    assert _era_entries(db, p) == []  # settlement reversed (no live entry)


def test_live_record_path_posts_settlement_not_p3(db):
    """The office backfilling a historical payment must succeed at record
    time — previously P3 at the pre-cutover date slammed the era lock."""
    inv = _era_invoice(db, total="300.00")
    _enable(db)
    post_opening_balance(db, inv, actor="t")
    p = _late_payment(db, inv, amount="300.00")
    entry = post_payment_received(db, p, inv, actor="t")  # must NOT raise
    db.commit()
    assert entry is not None
    assert entry.effective_at == CUTOVER
    assert invoice_ar_balance_cents(db, inv) == 0
    # And no P3 exists for it (cash untouched).
    p3 = db.scalars(select(GlJournalEntry).where(
        GlJournalEntry.source_id == str(p.id),
        GlJournalEntry.idempotency_key.like(f"payment:{p.id}:received:%"),
    )).all()
    assert p3 == []


def _live_settlement_cents(db, payment):
    """Equity-debit cents of the payment's LIVE era entry (0 if none)."""
    entries = _era_entries(db, payment)
    if not entries:
        return 0
    return max(int(line.amount_cents) for line in db.scalars(
        select(GlJournalLine).where(GlJournalLine.entry_id == entries[0].id)).all())


def test_out_of_order_live_records_never_overshoot(db):
    """2026-08-17 audit HIGH (executed repro): the record path posted only
    the current payment's slice, so an out-of-date-order record left the
    sibling's stale entry live — $130 settled against a $100 anchor, GL AR
    driven negative. The record path must whole-invoice resettle."""
    inv = _era_invoice(db, total="100.00")
    _enable(db)
    post_opening_balance(db, inv, actor="t")
    db.commit()

    # Office records the LATER-dated payment first…
    p1 = _late_payment(db, inv, amount="80.00", paid_on=date(2026, 6, 20))
    post_payment_received(db, p1, inv, actor="t")
    db.commit()
    assert _live_settlement_cents(db, p1) == 8000

    # …then learns of an EARLIER-dated one. It sorts first, so the
    # allocation moves: p2 takes $50, p1 shrinks to $50 — and p1's stale
    # $80 entry must be reversed in the same pass.
    p2 = _late_payment(db, inv, amount="50.00", paid_on=date(2026, 6, 10))
    post_payment_received(db, p2, inv, actor="t")
    db.commit()

    assert _live_settlement_cents(db, p2) == 5000
    assert _live_settlement_cents(db, p1) == 5000
    assert invoice_ar_balance_cents(db, inv) == 0  # never negative


def test_anchor_zero_late_payment_is_loud_not_silent(db, caplog):
    """2026-08-17 audit MEDIUM (executed repro): a late payment on an
    invoice settled at cutover allocated nothing and vanished without a
    trace. No entry is CORRECT (no anchor to correct) — silence is not."""
    import logging as _logging

    from gdx_dispatch.modules.ledger.backfill import reconciliation_report

    inv = _era_invoice(db, total="500.00")
    # Fully paid BEFORE cutover: the row existed at cutover, so the anchor
    # pool is zero and no P8 posts.
    old = Payment(id=uuid4(), invoice_id=inv.id, amount=Decimal("500.00"),
                  method="check", payment_date=date(2026, 6, 15), company_id=COMPANY)
    db.add(old)
    db.flush()
    old.created_at = datetime(2026, 6, 15, 10, 0)
    db.flush()
    _enable(db)
    assert post_opening_balance(db, inv, actor="t") is None

    late = _late_payment(db, inv, amount="500.00")
    with caplog.at_level(_logging.WARNING, logger="gdx_dispatch.modules.ledger.rules"):
        entry = post_payment_received(db, late, inv, actor="t")
    db.commit()

    assert entry is None  # nothing to correct — but never silently:
    assert any("era settlement shortfall" in r.message for r in caplog.records)
    report = reconciliation_report(db, COMPANY)
    shortfalls = report["era_settlement_shortfalls"]
    assert len(shortfalls) == 1
    assert shortfalls[0]["invoice_number"] == inv.invoice_number
    assert shortfalls[0]["uncovered_cents"] == 50000
    assert shortfalls[0]["allocated_cents"] == 0


def test_partial_allocation_shortfall_in_report(db):
    """The capped excess of a PARTIAL allocation is the same vanishing money
    — the report must carry the uncovered remainder, not just anchor-zero."""
    from gdx_dispatch.modules.ledger.backfill import reconciliation_report

    inv = _era_invoice(db, total="100.00")
    _enable(db)
    post_opening_balance(db, inv, actor="t")
    _late_payment(db, inv, amount="80.00", paid_on=date(2026, 6, 10))
    p2 = _late_payment(db, inv, amount="50.00", paid_on=date(2026, 6, 20))
    resettle_invoice_payments(db, inv, actor="t")
    db.commit()

    shortfalls = reconciliation_report(db, COMPANY)["era_settlement_shortfalls"]
    assert len(shortfalls) == 1
    assert shortfalls[0]["payment_id"] == str(p2.id)
    assert shortfalls[0]["allocated_cents"] == 2000
    assert shortfalls[0]["uncovered_cents"] == 3000


def test_mixed_naive_aware_created_at_sorts_cleanly(db):
    """2026-08-17 audit MEDIUM (executed repro): DB-loaded rows carry naive
    created_at on SQLite while fresh rows carry aware ones — with tied
    payment_dates the allocation sort compared them and TypeError'd at
    record time. The sort must normalize like _prior_nonvoided_paid_cents."""
    from datetime import timezone as _tz

    inv = _era_invoice(db, total="100.00")
    _enable(db)
    post_opening_balance(db, inv, actor="t")
    p1 = _late_payment(db, inv, amount="60.00", paid_on=date(2026, 6, 15))
    p2 = _late_payment(db, inv, amount="60.00", paid_on=date(2026, 6, 15))
    p1.created_at = POST                            # naive (DB-loaded shape)
    p2.created_at = POST.replace(tzinfo=_tz.utc)    # aware (fresh-row shape)
    db.flush()

    resettle_invoice_payments(db, inv, actor="t")   # must not raise
    db.commit()
    assert _live_settlement_cents(db, p1) + _live_settlement_cents(db, p2) == 10000
    assert invoice_ar_balance_cents(db, inv) == 0


def test_settlement_lines_carry_anchor_dimensions(db):
    """2026-08-17 audit LOW: the P8 anchor AR debit carries job_id +
    customer_id — the settlement's AR credit must match or per-job and
    per-customer AR never net to zero."""
    inv = _era_invoice(db)
    inv.job_id = uuid4()
    db.flush()
    _enable(db)
    post_opening_balance(db, inv, actor="t")
    p = _late_payment(db, inv, amount="1000.00")
    resettle_invoice_payments(db, inv, actor="t")
    db.commit()

    entry = _era_entries(db, p)[0]
    lines = db.scalars(select(GlJournalLine).where(GlJournalLine.entry_id == entry.id)).all()
    ar_line = next(line for line in lines if int(line.amount_cents) < 0)
    eq_line = next(line for line in lines if int(line.amount_cents) > 0)
    assert str(ar_line.job_id) == str(inv.job_id)
    assert str(ar_line.customer_id) == str(inv.customer_id)
    assert str(eq_line.customer_id) == str(inv.customer_id)


def test_post_cutover_payment_unaffected(db):
    inv = _era_invoice(db, total="400.00")
    _enable(db)
    post_opening_balance(db, inv, actor="t")
    p = Payment(id=uuid4(), invoice_id=inv.id, amount=Decimal("400.00"),
                method="check", payment_date=date(2026, 7, 15), company_id=COMPANY)
    db.add(p)
    db.flush()
    p.created_at = POST
    db.flush()
    entry = post_payment_received(db, p, inv, actor="t")
    db.commit()
    assert entry is not None
    assert "received" in entry.idempotency_key  # normal P3, own date
    assert entry.effective_at == date(2026, 7, 15)
