"""GDXA-505 — money dates are the shop's calendar day, not the UTC server's.

Every test runs at one frozen instant:

    03:00 UTC on 1 Oct 2026 = 22:00 on 30 Sep in America/Chicago.

Past UTC midnight, before shop midnight, and across a MONTH boundary, so a
date taken from the UTC clock lands in the wrong month as well as the wrong
day. Each test below fails on the old code, which dated the event 1 Oct (or
started "this month" on 1 Oct), except the one marked as a guard: that one
pins the rule that an entry already posted on its UTC date keeps it, so a GL
replay cannot post the same money twice.
"""
from __future__ import annotations

import datetime as dt
import secrets
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from freezegun import freeze_time
from sqlalchemy import event, select

from gdx_dispatch.models.tenant_models import AppSettings, Invoice, InvoiceLine, Payment

# Imported here, not inside a test: building the router's pydantic models
# under freeze_time sees freezegun's FakeDate and fails schema generation.
from gdx_dispatch.modules.ledger import reports
from gdx_dispatch.modules.ledger import router as ledger_router
from gdx_dispatch.modules.ledger.models import GlJournalEntry, GlPeriodLock
from gdx_dispatch.modules.ledger.service import ensure_gl_seed, transition_invoice_status
from gdx_dispatch.routers.estimates import _compute_price_drift
from gdx_dispatch.routers.invoices import (
    ApplyCreditIn,
    CreditMemoIn,
    PaymentCreateIn,
    RefundIn,
    apply_customer_credit,
    billing_summary,
    issue_credit_memo,
    process_refund,
    record_payment,
)

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"tenant_id": COMPANY, "sub": "tester"}
EVENING_UTC = "2026-10-01 03:00:00"
FROZEN_NOW = datetime(2026, 10, 1, 3, 0)  # naive UTC, as SQLite hands it back
SHOP_DAY = date(2026, 9, 30)
UTC_DAY = date(2026, 10, 1)


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    tenant_db.add(AppSettings(timezone="America/Chicago"))
    ensure_gl_seed(tenant_db, COMPANY)
    tenant_db.commit()
    return tenant_db


def _enable(db):
    settings = ensure_gl_seed(db, COMPANY)
    settings.ledger_posting_enabled = True
    db.commit()


def _invoice(db, total="100.00", status="draft", customer_id=None, invoice_date=date(2026, 9, 1)):
    inv = Invoice(
        id=uuid4(),
        customer_id=customer_id or uuid4(),
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status=status,
        subtotal=Decimal(total),
        tax_amount=Decimal("0.00"),
        total=Decimal(total),
        balance_due=Decimal(total),
        invoice_date=invoice_date,
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


def _sent_invoice(db, total="100.00", customer_id=None):
    inv = _invoice(db, total=total, customer_id=customer_id)
    transition_invoice_status(db, inv, "sent")
    db.commit()
    return inv


def _pay(db, inv, amount, **kw):
    return record_payment(inv.id, PaymentCreateIn(amount=amount, method="cash", **kw), _=USER, db=db)


def _entries(db, marker):
    return [
        e for e in db.scalars(select(GlJournalEntry).order_by(GlJournalEntry.entry_no)).all()
        if e.idempotency_key and f":{marker}:" in e.idempotency_key
    ]


# ─── routers/invoices.py: recording a payment ───────────────────────────


@freeze_time(EVENING_UTC)
def test_payment_without_a_date_is_dated_the_shop_day(db):
    inv = _sent_invoice(db)
    _pay(db, inv, 40.0)
    (payment,) = db.scalars(select(Payment).where(Payment.invoice_id == inv.id)).all()
    assert payment.payment_date == SHOP_DAY


@freeze_time(EVENING_UTC)
def test_payment_dated_the_utc_day_is_refused_as_future(db):
    """1 Oct is tomorrow at the shop: a post-dated check, not received cash."""
    inv = _sent_invoice(db)
    with pytest.raises(HTTPException) as exc:
        _pay(db, inv, 40.0, date=UTC_DAY)
    assert exc.value.status_code == 422
    assert "future" in exc.value.detail


@freeze_time(EVENING_UTC)
def test_payment_dated_the_shop_day_keeps_the_precise_paid_at(db):
    """Same-day payment: not a backdate, so paid_at stays the real instant,
    not a date-only UTC-midnight stamp on the 30th."""
    inv = _sent_invoice(db)
    _pay(db, inv, 100.0, date=SHOP_DAY)
    db.refresh(inv)
    assert inv.status == "paid"
    assert inv.paid_at.replace(tzinfo=None) == FROZEN_NOW


@freeze_time(EVENING_UTC)
def test_paid_this_month_starts_on_the_shops_month(db):
    """Reads the bound month start, not the KPI value: SQLite casts a stored
    timestamp to DATE with numeric affinity (``2026``), so the KPI is 0 here
    whatever the bound; production is Postgres."""
    bound = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if "paid_at" in statement and "sum(" in statement.lower():
            bound.extend(str(p) for p in (parameters or ()))

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _capture)
    try:
        billing_summary(SimpleNamespace(), _=USER, db=db)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    assert "2026-09-01" in bound
    assert "2026-10-01" not in bound


# ─── core/payments.py ───────────────────────────────────────────────────


@freeze_time(EVENING_UTC)
def test_card_payment_is_dated_the_shop_day(db):
    from gdx_dispatch.core.payments import _mark_invoice_paid

    inv = _sent_invoice(db)
    _mark_invoice_paid(inv, db, external_ref="pi_gdxa505", amount=100.0)
    (payment,) = db.scalars(select(Payment).where(Payment.invoice_id == inv.id)).all()
    assert payment.payment_date == SHOP_DAY


def test_locked_period_webhook_void_unwinds_into_the_shops_open_day(db):
    from gdx_dispatch.core.payments import handle_payment_webhook

    _enable(db)
    inv = _sent_invoice(db, total="500.00")
    record_payment(
        inv.id,
        PaymentCreateIn(amount=500.0, method="card", reference="pi_505_lock", date=date(2026, 9, 15)),
        _=USER, db=db,
    )
    (p3,) = _entries(db, "received")
    db.add(GlPeriodLock(company_id=COMPANY, lock_date=p3.effective_at))
    db.commit()

    with freeze_time(EVENING_UTC):
        handle_payment_webhook(
            {"type": "charge.refunded", "data": {"object": {
                "id": "ch_505", "payment_intent": "pi_505_lock",
                "amount": 50_000, "amount_refunded": 50_000, "refunded": True,
            }}},
            db,
        )
    reversal = [e for e in db.scalars(select(GlJournalEntry)).all() if e.reverses_entry_id == p3.id]
    assert len(reversal) == 1
    assert reversal[0].effective_at == SHOP_DAY


# ─── modules/ledger/rules.py: adjustment postings ───────────────────────


@freeze_time(EVENING_UTC)
def test_credit_memo_posts_on_the_shop_day(db):
    _enable(db)
    inv = _sent_invoice(db)
    issue_credit_memo(str(inv.id), CreditMemoIn(amount=30.0, reason="warranty"), db=db, _=USER)
    (entry,) = _entries(db, "credit_memo")
    assert entry.effective_at == SHOP_DAY


@freeze_time(EVENING_UTC)
def test_refund_posts_on_the_shop_day(db):
    _enable(db)
    inv = _sent_invoice(db)
    _pay(db, inv, 100.0)
    process_refund(
        str(inv.id), RefundIn(amount=30.0, reason="warranty", refund_method="check"), db=db, _=USER
    )
    (entry,) = _entries(db, "refund")
    assert entry.effective_at == SHOP_DAY


@freeze_time(EVENING_UTC)
def test_credit_application_posts_on_the_shop_day(db):
    _enable(db)
    customer_id = uuid4()
    overpaid = _sent_invoice(db, customer_id=customer_id)
    _pay(db, overpaid, 150.0, allow_overpayment=True)
    target = _sent_invoice(db, total="80.00", customer_id=customer_id)
    apply_customer_credit(target.id, ApplyCreditIn(amount=50.0), db=db, _=USER)
    (entry,) = _entries(db, "credit_applied")
    assert entry.effective_at == SHOP_DAY


@freeze_time(EVENING_UTC)
def test_an_adjustment_already_posted_on_its_utc_day_keeps_it_on_replay(db, monkeypatch):
    """Guard, not a falsifier: new postings only. A credit memo posted under
    the old rule (UTC day) and replayed by the backfill or a resettle must
    find its own key, not mint a second entry on the shop day."""
    from gdx_dispatch.models.tenant_models import InvoiceAdjustment
    from gdx_dispatch.modules.ledger import rules

    _enable(db)
    inv = _sent_invoice(db)
    real = rules._shop_effective_at
    monkeypatch.setattr(
        rules, "_shop_effective_at",
        lambda session, source_type, source_id, event, ts: ts.date(),
    )
    issue_credit_memo(str(inv.id), CreditMemoIn(amount=30.0, reason="warranty"), db=db, _=USER)
    (legacy,) = _entries(db, "credit_memo")
    assert legacy.effective_at == UTC_DAY, "setup: posted the way the old code did"
    monkeypatch.setattr(rules, "_shop_effective_at", real)

    adjustment = db.scalars(select(InvoiceAdjustment).where(InvoiceAdjustment.invoice_id == inv.id)).one()
    replayed = rules.post_credit_memo(db, adjustment, inv, actor="replay")
    db.commit()
    assert replayed.id == legacy.id
    assert len(_entries(db, "credit_memo")) == 1


# ─── modules/ledger/rules.py: opening-era voids ─────────────────────────


def _opening_era(db):
    from gdx_dispatch.modules.ledger.rules import post_opening_balance

    settings = ensure_gl_seed(db, COMPANY)
    settings.cutover_month = date(2026, 7, 1)
    db.commit()
    inv = _invoice(db, total="1000.00", status="sent", invoice_date=date(2026, 6, 5))
    pay = Payment(
        id=uuid4(), invoice_id=inv.id, amount=Decimal("200.00"), method="check",
        payment_date=date(2026, 6, 10), company_id=COMPANY,
    )
    db.add(pay)
    db.flush()
    pay.created_at = datetime(2026, 6, 10, 12, 0)
    db.flush()
    post_opening_balance(db, inv, actor="t")
    _enable(db)
    return inv, pay


def test_opening_era_payment_void_posts_on_the_shop_day(db):
    from gdx_dispatch.modules.ledger.rules import EVENT_OPENING_PAYMENT_VOID, resettle_invoice_payments

    inv, pay = _opening_era(db)
    with freeze_time(EVENING_UTC):
        pay.voided_at = FROZEN_NOW
        db.flush()
        resettle_invoice_payments(db, inv, actor="t")
        (entry,) = _entries(db, EVENT_OPENING_PAYMENT_VOID)
        assert entry.effective_at == SHOP_DAY
        # Still idempotent: the second resettle finds the live entry.
        resettle_invoice_payments(db, inv, actor="t")
        assert len(_entries(db, EVENT_OPENING_PAYMENT_VOID)) == 1


def test_opening_era_invoice_void_posts_on_the_shop_day(db):
    from gdx_dispatch.modules.ledger.rules import EVENT_OPENING_VOID, settle_opening_on_void

    inv, _pay_row = _opening_era(db)
    with freeze_time(EVENING_UTC):
        transition_invoice_status(db, inv, "void", actor="t")
        settle_opening_on_void(db, inv, actor="t")
        (entry,) = _entries(db, EVENT_OPENING_VOID)
        assert entry.effective_at == SHOP_DAY


# ─── modules/ledger/router.py: report defaults ──────────────────────────


@freeze_time(EVENING_UTC)
def test_report_defaults_are_the_shops_day_and_month(db, monkeypatch):
    seen = {}
    monkeypatch.setattr(reports, "pnl_accrual", lambda db, cid, start, end: seen.update(pnl=(start, end)))
    monkeypatch.setattr(reports, "trial_balance", lambda db, cid, as_of: seen.update(tb=as_of))
    monkeypatch.setattr(reports, "balance_sheet", lambda db, cid, as_of: seen.update(bs=as_of))

    ledger_router.report_pnl(start=None, end=None, basis="accrual", db=db, user=USER, _perm=None)
    ledger_router.report_trial_balance(as_of=None, db=db, user=USER, _perm=None)
    ledger_router.report_balance_sheet(as_of=None, db=db, user=USER, _perm=None)

    assert seen == {"pnl": (date(2026, 9, 1), SHOP_DAY), "tb": SHOP_DAY, "bs": SHOP_DAY}


# ─── modules/tax, modules/billing_terms, core/billing_lanes ─────────────


@freeze_time(EVENING_UTC)
def test_tax_exemption_window_reads_the_shop_day(db):
    from gdx_dispatch.modules.tax.models import TaxExemption
    from gdx_dispatch.modules.tax.service import is_customer_exempt

    TaxExemption.__table__.create(bind=db.get_bind(), checkfirst=True)
    ends_today, starts_tomorrow = uuid4(), uuid4()
    db.add(TaxExemption(customer_id=ends_today, exempt=True, exempt_until=SHOP_DAY))
    db.add(TaxExemption(customer_id=starts_tomorrow, exempt=True, exempt_from=UTC_DAY))
    db.commit()
    assert is_customer_exempt(db, ends_today) is True
    assert is_customer_exempt(db, starts_tomorrow) is False


@freeze_time(EVENING_UTC)
def test_billing_terms_date_from_the_shop_day(db):
    from gdx_dispatch.modules.billing_terms.service import EffectiveTerms

    terms = EffectiveTerms(
        payment_terms_days=30, early_pay_discount_percent=Decimal("2"),
        early_pay_discount_days=10, late_fee_flat_amount=None, late_fee_percent=None,
        late_fee_grace_days=0, interest_rate_monthly_percent=None, interest_grace_days=0,
    )
    stamp = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)
    assert terms.due_date(None, db=db) == SHOP_DAY + dt.timedelta(days=30)
    assert terms.due_date(stamp, db=db) == SHOP_DAY + dt.timedelta(days=30)
    assert terms.early_pay_deadline(None, db=db) == SHOP_DAY + dt.timedelta(days=10)
    assert terms.early_pay_deadline(stamp, db=db) == SHOP_DAY + dt.timedelta(days=10)


@freeze_time(EVENING_UTC)
def test_labor_row_retired_today_is_billable_until_shop_midnight(db):
    from gdx_dispatch.core.billing_lanes import install_labor_line
    from gdx_dispatch.models.labor_pricing import LaborPriceItem

    LaborPriceItem.__table__.create(bind=db.get_bind(), checkfirst=True)
    item = LaborPriceItem(
        id=uuid4(), description="16x7 Sectional Install", service_type="install",
        flat_price=Decimal("650"), assumed_man_hours=Decimal("6.5"),
        default_crew_size=1, min_wall_clock_minutes=15, active=True,
        effective_from=date(2026, 1, 1), effective_to=SHOP_DAY, sort_order=1,
    )
    db.add(item)
    db.commit()
    assert install_labor_line(db, str(item.id)) is not None
    # The estimate drift check promises the SAME retired-row definition, so
    # the evening it bills the row it must not call the row retired.
    line = SimpleNamespace(
        id=uuid4(), description=item.description, labor_price_item_id=item.id,
        unit_price=Decimal("650"), quantity=1,
    )
    assert _compute_price_drift(SimpleNamespace(lines=[line]), db) == []
