"""GL Phase 1 (S10) — cutover, P8 opening balances, backfill (spec §5.7).

Plan gates: P8 posts one AR anchor per open pre-cutover invoice (customer
dimension, credit 3950) using the timestamp-anchored opening formula;
anchors are never reversed-and-reposted (edits no-op the repost path; void
posts its own settlement entry); opening-era payments stay inside the P8
amount (resettle skips them; post-cutover voids post a compensator); the
backfill phases are re-runnable with identical keys; the cash-basis
proration primitive passes the Intuit worked example.
"""
from __future__ import annotations

import datetime as dt
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from gdx_dispatch.models.tenant_models import (
    Expense,
    Invoice,
    InvoiceAdjustment,
    InvoiceLine,
    Payment,
)
from gdx_dispatch.modules.ledger import backfill
from gdx_dispatch.modules.ledger.cash_basis import (
    invoice_components,
    prorate_event_cents,
)
from gdx_dispatch.modules.ledger.coa import LedgerConfigError, resolve_role_account
from gdx_dispatch.modules.ledger.models import (
    ROLE_AR,
    ROLE_OPENING_EQUITY,
    GlJournalEntry,
    GlJournalLine,
    GlPeriodLock,
)
from gdx_dispatch.modules.ledger.money import allocate
from gdx_dispatch.modules.ledger.rules import (
    EVENT_OPENING,
    EVENT_OPENING_PAYMENT_VOID,
    EVENT_OPENING_VOID,
    invoice_ar_balance_cents,
    opening_balance_cents,
    post_opening_balance,
    repost_invoice_issuance,
    resettle_invoice_payments,
    settle_opening_on_void,
)
from gdx_dispatch.modules.ledger.service import (
    ensure_gl_seed,
    transition_invoice_status,
)

COMPANY = "11111111-1111-1111-1111-111111111111"
CUTOVER = date(2026, 7, 1)
PRE = datetime(2026, 6, 10, 12, 0)
POST = datetime(2026, 7, 10, 12, 0)


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    settings = ensure_gl_seed(tenant_db, COMPANY)
    settings.cutover_month = CUTOVER
    tenant_db.commit()
    return tenant_db


def _enable(db):
    settings = ensure_gl_seed(db, COMPANY)
    settings.ledger_posting_enabled = True
    db.commit()


def _invoice(db, *, total="1000.00", tax="0.00", status="sent", invoice_date=date(2026, 6, 5)):
    inv = Invoice(
        id=uuid4(),
        customer_id=uuid4(),
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status=status,
        subtotal=Decimal(total) - Decimal(tax),
        tax_amount=Decimal(tax),
        total=Decimal(total),
        balance_due=Decimal(total),
        amount_paid=Decimal("0.00"),
        invoice_date=invoice_date,
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=inv.id,
            description="Spring replacement",
            quantity=1,
            unit_price=Decimal(total) - Decimal(tax),
            line_total=Decimal(total) - Decimal(tax),
            company_id=COMPANY,
        )
    )
    db.flush()
    return inv


def _payment(db, invoice, *, amount="200.00", created_at=PRE, voided_at=None, method="check"):
    payment = Payment(
        id=uuid4(),
        invoice_id=invoice.id,
        amount=Decimal(amount),
        method=method,
        payment_date=created_at.date(),
        company_id=COMPANY,
    )
    db.add(payment)
    db.flush()
    payment.created_at = created_at
    payment.voided_at = voided_at
    db.flush()
    return payment


def _credit(db, invoice, *, amount="100.00", created_at=PRE, kind="credit_memo"):
    adj = InvoiceAdjustment(
        id=uuid4(),
        invoice_id=invoice.id,
        kind=kind,
        amount=Decimal(amount),
        reason="goodwill",
        company_id=COMPANY,
    )
    db.add(adj)
    db.flush()
    adj.created_at = created_at
    db.flush()
    return adj


def _entries(db, event=None):
    q = select(GlJournalEntry).where(GlJournalEntry.company_id == COMPANY)
    rows = db.scalars(q).all()
    if event:
        rows = [r for r in rows if f":{event}:" in (r.idempotency_key or "")]
    return rows


def _account_balance(db, role):
    account = resolve_role_account(db, COMPANY, role)
    rows = db.execute(
        select(GlJournalLine.amount_cents)
        .join(GlJournalEntry, GlJournalLine.entry_id == GlJournalEntry.id)
        .where(GlJournalLine.account_id == account.id)
    ).all()
    return sum(a for (a,) in rows)


# ---------------------------------------------------------------------------
# Cash-basis proration primitive — the Intuit worked example (spec §6)
# ---------------------------------------------------------------------------

def test_intuit_worked_example_tax_split():
    # $1,060 invoice, two gross lines $424 + $636 → the $60 tax attributes
    # $24 + $36 (Intuit's own cash-basis derivation example).
    assert allocate(60_00, [400_00, 600_00]) == [24_00, 36_00]


def test_prorate_full_partial_overpaid_and_refund():
    components = [400_00, 600_00, 60_00]  # two revenue lines + tax
    total = 1060_00
    assert prorate_event_cents(components, 1060_00, total) == [400_00, 600_00, 60_00]
    # 50% payment recognizes half of each component, sum-preserving.
    half = prorate_event_cents(components, 530_00, total)
    assert half == [200_00, 300_00, 30_00]
    assert sum(half) == 530_00
    # Overpayment: ratio caps at 1.0 — the excess is 2300, never revenue.
    assert prorate_event_cents(components, 2000_00, total) == [400_00, 600_00, 60_00]
    # Refunds prorate negatively at their own date.
    assert prorate_event_cents(components, -530_00, total) == [-200_00, -300_00, -30_00]
    assert prorate_event_cents(components, 0, total) == [0, 0, 0]


def test_invoice_components_reads_operational_lines(db):
    invoice = _invoice(db, total="1060.00", tax="60.00")
    comps = invoice_components(db, invoice)
    assert sum(c.cents for c in comps) == 1060_00
    assert any(c.role is not None or c.account_id is not None for c in comps)


# ---------------------------------------------------------------------------
# Opening formula + P8 posting
# ---------------------------------------------------------------------------

def test_opening_balance_formula_timestamp_anchored(db):
    invoice = _invoice(db, total="1000.00")
    _payment(db, invoice, amount="200.00", created_at=PRE)
    _credit(db, invoice, amount="100.00", created_at=PRE)
    # post-cutover rows don't count — they replay as their own entries
    _payment(db, invoice, amount="50.00", created_at=POST)
    _credit(db, invoice, amount="25.00", created_at=POST)
    # a payment voided BEFORE cutover never counted at cutover
    _payment(db, invoice, amount="70.00", created_at=PRE, voided_at=PRE + timedelta(days=1))
    # a payment voided AFTER cutover still counted at cutover
    _payment(db, invoice, amount="30.00", created_at=PRE, voided_at=POST)

    assert opening_balance_cents(db, invoice, CUTOVER) == 1000_00 - 200_00 - 100_00 - 30_00


def test_post_opening_balance_posts_anchor_idempotently(db):
    invoice = _invoice(db, total="1000.00")
    _payment(db, invoice, amount="200.00", created_at=PRE)

    entry = post_opening_balance(db, invoice, actor="test")
    assert entry is not None
    assert entry.effective_at == CUTOVER
    again = post_opening_balance(db, invoice, actor="test")
    assert again.id == entry.id
    assert len(_entries(db, EVENT_OPENING)) == 1
    assert _account_balance(db, ROLE_AR) == 800_00
    assert _account_balance(db, ROLE_OPENING_EQUITY) == -800_00


def test_post_opening_balance_skips_nonqualifying(db):
    settled = _invoice(db, total="100.00")
    _payment(db, settled, amount="100.00", created_at=PRE)
    assert post_opening_balance(db, settled, actor="t") is None

    post_cutover = _invoice(db, total="100.00", invoice_date=date(2026, 7, 5))
    assert post_opening_balance(db, post_cutover, actor="t") is None

    draft = _invoice(db, total="100.00", status="draft")
    assert post_opening_balance(db, draft, actor="t") is None


def test_repost_never_posts_p1_over_an_anchor(db):
    invoice = _invoice(db, total="1000.00")
    post_opening_balance(db, invoice, actor="t")
    _enable(db)
    before = len(_entries(db))
    repost_invoice_issuance(db, invoice, actor="t")
    assert len(_entries(db)) == before  # strict no-op — no fresh P1


# ---------------------------------------------------------------------------
# Payments against an anchored invoice
# ---------------------------------------------------------------------------

def test_resettle_skips_opening_era_payments_but_posts_new_ones(db):
    invoice = _invoice(db, total="1000.00")
    _payment(db, invoice, amount="200.00", created_at=PRE)
    post_opening_balance(db, invoice, actor="t")
    _enable(db)
    _payment(db, invoice, amount="300.00", created_at=POST)

    resettle_invoice_payments(db, invoice, actor="t")
    payment_entries = _entries(db, "received")
    assert len(payment_entries) == 1  # only the post-cutover payment posted
    # AR: 800 opening − 300 payment
    assert _account_balance(db, ROLE_AR) == 500_00


def test_voiding_an_opening_era_payment_posts_compensator(db):
    invoice = _invoice(db, total="1000.00")
    opening_pay = _payment(db, invoice, amount="200.00", created_at=PRE)
    post_opening_balance(db, invoice, actor="t")  # anchor = 800
    _enable(db)

    opening_pay.voided_at = datetime(2026, 7, 12, 9, 0)
    db.flush()
    resettle_invoice_payments(db, invoice, actor="t")

    comp = _entries(db, EVENT_OPENING_PAYMENT_VOID)
    assert len(comp) == 1
    # customer owes the $200 again: AR = 800 + 200
    assert _account_balance(db, ROLE_AR) == 1000_00
    # idempotent on a second resettle
    resettle_invoice_payments(db, invoice, actor="t")
    assert len(_entries(db, EVENT_OPENING_PAYMENT_VOID)) == 1


# ---------------------------------------------------------------------------
# Void of an anchored invoice
# ---------------------------------------------------------------------------

def test_settle_opening_on_void_clears_remaining_ar(db):
    invoice = _invoice(db, total="1000.00")
    _payment(db, invoice, amount="200.00", created_at=PRE)
    post_opening_balance(db, invoice, actor="t")  # anchor 800
    _enable(db)
    pay = _payment(db, invoice, amount="300.00", created_at=POST)
    resettle_invoice_payments(db, invoice, actor="t")  # AR 500

    # the endpoint's flow: payments voided first, then the status transition
    pay.voided_at = datetime(2026, 7, 15, 9, 0)
    db.flush()
    resettle_invoice_payments(db, invoice, actor="t")  # AR back to 800
    transition_invoice_status(db, invoice, "void", actor="t")
    settle_opening_on_void(db, invoice, actor="t")
    assert len(_entries(db, EVENT_OPENING_VOID)) == 1
    assert invoice_ar_balance_cents(db, invoice) == 0
    # second call: existence-guarded (voids carry no stable timestamp)
    settle_opening_on_void(db, invoice, actor="t")
    assert len(_entries(db, EVENT_OPENING_VOID)) == 1


def test_settle_opening_on_void_noop_without_anchor(db):
    invoice = _invoice(db, total="500.00", invoice_date=date(2026, 7, 5))
    _enable(db)
    transition_invoice_status(db, invoice, "void", actor="t")
    settle_opening_on_void(db, invoice, actor="t")
    assert _entries(db, EVENT_OPENING_VOID) == []


# ---------------------------------------------------------------------------
# Backfill phases
# ---------------------------------------------------------------------------

def test_run_opening_posts_anchors_and_era_lock(db):
    open_pre = _invoice(db, total="1000.00")
    _payment(db, open_pre, amount="400.00", created_at=PRE)
    settled_pre = _invoice(db, total="100.00")
    _payment(db, settled_pre, amount="100.00", created_at=PRE)
    _invoice(db, total="700.00", invoice_date=date(2026, 7, 3))  # post-cutover

    result = backfill.run_opening(db, COMPANY)
    assert result.posted == 1
    lock = db.scalar(select(GlPeriodLock).where(GlPeriodLock.company_id == COMPANY))
    assert lock is not None and lock.lock_date == CUTOVER - timedelta(days=1)

    # re-run: no new entries, no second lock
    entries_before = len(_entries(db))
    again = backfill.run_opening(db, COMPANY)
    assert len(_entries(db)) == entries_before
    assert len(db.scalars(select(GlPeriodLock)).all()) == 1
    assert again.posted == 1  # idempotent success counts as posted


def test_run_replay_requires_flag(db):
    with pytest.raises(LedgerConfigError):
        backfill.run_replay(db, COMPANY)


def test_replay_end_to_end_reconciles(db):
    # Pre-cutover open invoice with an opening-era payment.
    anchored = _invoice(db, total="1000.00")
    _payment(db, anchored, amount="200.00", created_at=PRE)
    # Post-cutover invoice with a payment and a credit memo.
    fresh = _invoice(db, total="530.00", invoice_date=date(2026, 7, 8))
    _payment(db, fresh, amount="130.00", created_at=POST)
    _credit(db, fresh, amount="100.00", created_at=POST)
    # Post-cutover expense.
    expense = Expense(
        id=uuid4(),
        vendor="Fuel Co",
        description="fuel",
        amount=Decimal("60.00"),
        category="fuel",
        date=date(2026, 7, 9),
        company_id=COMPANY,
    )
    db.add(expense)
    db.flush()

    backfill.run_opening(db, COMPANY)
    _enable(db)
    result = backfill.run_replay(db, COMPANY)
    assert result.locked == []

    # Anchored: opening 800; fresh: P1 530 − payment 130 − credit 100 = 300.
    assert invoice_ar_balance_cents(db, anchored) == 800_00
    assert invoice_ar_balance_cents(db, fresh) == 300_00
    assert _account_balance(db, ROLE_AR) == 1100_00

    # Re-run replay: same keys, nothing new.
    entries_before = len(_entries(db))
    backfill.run_replay(db, COMPANY)
    assert len(_entries(db)) == entries_before

    # Reconciliation against the REAL recomputed operational balances —
    # audit round 1: forging balance_due here asserted the test's own inputs.
    from gdx_dispatch.routers.invoices import _recalculate_invoice

    _recalculate_invoice(anchored, db)
    _recalculate_invoice(fresh, db)
    assert anchored.balance_due == Decimal("800.00")
    assert fresh.balance_due == Decimal("300.00")
    report = backfill.reconciliation_report(db, COMPANY)
    assert report["mismatches"] == []
    assert report["totals"]["operational_ar_cents"] == 1100_00
    assert report["totals"]["gl_ar_account_cents"] == 1100_00


def test_backdated_issuance_anchors_instead_of_posting_p1(db):
    # Era membership is by DATE (audit round 1): an invoice issued after
    # cutover but DATED into the pre-cutover era claims the sale for the
    # QBO-era books — it anchors as P8 at the cutover date instead of
    # posting P1 into the locked era (or crashing on the lock).
    backfill.run_opening(db, COMPANY)
    late = _invoice(db, total="250.00", invoice_date=date(2026, 6, 20))
    _enable(db)
    result = backfill.run_replay(db, COMPANY)
    assert result.locked == []
    assert _entries(db, "issued") == []
    assert len(_entries(db, EVENT_OPENING)) == 1
    assert invoice_ar_balance_cents(db, late) == 250_00


def test_settled_era_invoice_void_flow_needs_no_lock_override(db):
    # THE audit-round-1 counterexample: a pre-cutover invoice fully PAID at
    # cutover has no anchor. A bounced check found post-cutover voids its
    # payment — that must post the compensator (AR reopens), never try to
    # post fresh P1/P3 into the locked era.
    invoice = _invoice(db, total="400.00")
    pay = _payment(db, invoice, amount="400.00", created_at=PRE)
    backfill.run_opening(db, COMPANY)  # settled → no anchor, era locked
    assert _entries(db, EVENT_OPENING) == []
    _enable(db)

    pay.voided_at = datetime(2026, 7, 12, 9, 0)
    db.flush()
    resettle_invoice_payments(db, invoice, actor="t")  # must not raise
    assert _entries(db, "received") == []  # era payment never posts P3
    assert len(_entries(db, EVENT_OPENING_PAYMENT_VOID)) == 1
    assert invoice_ar_balance_cents(db, invoice) == 400_00  # owed again

    # a full replay over this state stays clean too
    result = backfill.run_replay(db, COMPANY)
    assert result.locked == []
    assert _entries(db, "issued") == []

    # and voiding the invoice itself clears the reopened AR
    transition_invoice_status(db, invoice, "void", actor="t")
    settle_opening_on_void(db, invoice, actor="t")
    assert invoice_ar_balance_cents(db, invoice) == 0


def test_replay_collects_period_locked_events_instead_of_crashing(db):
    # The remaining genuine lock case: a POST-cutover invoice whose payment
    # is backdated into the closed era (payment_date < cutover but the row
    # was born post-cutover). Replay refuses it into the report, not a crash.
    backfill.run_opening(db, COMPANY)
    fresh = _invoice(db, total="300.00", invoice_date=date(2026, 7, 5))
    backdated = _payment(db, fresh, amount="100.00", created_at=POST)
    backdated.payment_date = date(2026, 6, 15)
    db.flush()
    _enable(db)
    result = backfill.run_replay(db, COMPANY)
    assert len(result.locked) == 1
    assert "payments" in result.locked[0]
    assert _entries(db, "received") == []  # nothing posted into the locked era
