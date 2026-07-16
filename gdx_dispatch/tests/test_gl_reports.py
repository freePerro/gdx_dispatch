"""GL Phase 1 (S11) — reports + journal browser (spec §6, §9).

Plan gates: trial balance renders zero-proof; P&L accrual reads the GL and
cash derives at report time (cumulative per-invoice walk, cap at total,
refunds negative, era invoices prorate off operational lines); balance
sheet computes the retained-earnings rollup and balances by construction;
the journal drills entry → lines → source document (expense → receipts).
"""
from __future__ import annotations

import secrets
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from gdx_dispatch.models.tenant_models import (
    Expense,
    Invoice,
    InvoiceAdjustment,
    InvoiceLine,
    Payment,
)
from gdx_dispatch.modules.ledger import reports
from gdx_dispatch.modules.ledger.models import ExpenseReceipt, GlAccount
from gdx_dispatch.modules.ledger.router import (
    journal_browser,
    report_balance_sheet,
    report_pnl,
    report_trial_balance,
)
from gdx_dispatch.modules.ledger.rules import (
    post_expense_recorded,
    post_payment_received,
)
from gdx_dispatch.modules.ledger.service import (
    ensure_gl_seed,
    transition_invoice_status,
)

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"tenant_id": COMPANY, "sub": "tester"}
CUTOVER = date(2026, 7, 1)


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    settings = ensure_gl_seed(tenant_db, COMPANY)
    settings.ledger_posting_enabled = True
    tenant_db.commit()
    return tenant_db


def _account_by_code(db, code):
    return db.scalars(
        select(GlAccount).where(
            GlAccount.company_id == COMPANY, GlAccount.code == code
        )
    ).one()


def _invoice(db, *, lines, tax="0.00", invoice_date=date(2026, 7, 8), issue=True):
    total = sum(Decimal(amount) for amount, _cat in lines) + Decimal(tax)
    inv = Invoice(
        id=uuid4(),
        customer_id=uuid4(),
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status="draft",
        subtotal=total - Decimal(tax),
        tax_amount=Decimal(tax),
        total=total,
        balance_due=total,
        amount_paid=Decimal("0.00"),
        invoice_date=invoice_date,
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    for amount, category in lines:
        db.add(
            InvoiceLine(
                invoice_id=inv.id,
                description=f"line {category or 'general'}",
                quantity=1,
                unit_price=Decimal(amount),
                line_total=Decimal(amount),
                category=category,
                company_id=COMPANY,
            )
        )
    db.flush()
    if issue:
        transition_invoice_status(db, inv, "sent", actor="t")
        db.flush()
    return inv


def _pay(db, invoice, *, amount, payment_date):
    payment = Payment(
        id=uuid4(),
        invoice_id=invoice.id,
        amount=Decimal(amount),
        method="check",
        payment_date=payment_date,
        company_id=COMPANY,
    )
    db.add(payment)
    db.flush()
    post_payment_received(db, payment, invoice, actor="t")
    db.flush()
    return payment


def _map_revenue_categories(db):
    settings = ensure_gl_seed(db, COMPANY)
    settings.revenue_category_account_map = {
        "install": str(_account_by_code(db, "4100").id),
        "parts": str(_account_by_code(db, "4200").id),
    }
    db.commit()


# ---------------------------------------------------------------------------
# Trial balance
# ---------------------------------------------------------------------------

def test_trial_balance_zero_proof(db):
    invoice = _invoice(db, lines=[("500.00", None)], tax="30.00")
    _pay(db, invoice, amount="200.00", payment_date=date(2026, 7, 9))

    tb = reports.trial_balance(db, COMPANY, as_of=date(2026, 7, 31))
    assert tb["totals"]["zero_proof_cents"] == 0
    assert tb["totals"]["debit_cents"] == tb["totals"]["credit_cents"]
    by_code = {r["code"]: r for r in tb["rows"]}
    assert by_code["1200"]["debit_cents"] == 330_00  # AR: 530 − 200
    assert by_code["4000"]["credit_cents"] == 500_00
    assert by_code["2100"]["credit_cents"] == 30_00
    assert by_code["1050"]["debit_cents"] == 200_00  # check → undeposited

    # as-of BEFORE the activity: empty report, still zero-proof
    earlier = reports.trial_balance(db, COMPANY, as_of=date(2026, 6, 1))
    assert earlier["rows"] == []


# ---------------------------------------------------------------------------
# P&L — accrual window + cash derivation
# ---------------------------------------------------------------------------

def test_pnl_accrual_respects_window(db):
    _invoice(db, lines=[("400.00", None)], invoice_date=date(2026, 6, 10))
    _invoice(db, lines=[("700.00", None)], invoice_date=date(2026, 7, 10))

    july = reports.pnl_accrual(db, COMPANY, start=date(2026, 7, 1), end=date(2026, 7, 31))
    assert july["totals"]["revenue_cents"] == 700_00
    june = reports.pnl_accrual(db, COMPANY, start=date(2026, 6, 1), end=date(2026, 6, 30))
    assert june["totals"]["revenue_cents"] == 400_00


def test_pnl_cash_prorates_partial_payment_across_components(db):
    _map_revenue_categories(db)
    invoice = _invoice(
        db,
        lines=[("400.00", "install"), ("600.00", "parts")],
        tax="60.00",
        invoice_date=date(2026, 7, 2),
    )
    _pay(db, invoice, amount="530.00", payment_date=date(2026, 7, 15))

    cash = reports.pnl_cash(db, COMPANY, start=date(2026, 7, 1), end=date(2026, 7, 31))
    by_code = {r["code"]: r["amount_cents"] for r in cash["revenue"]}
    # 50% of each component; tax share (30_00) is a liability, NOT revenue
    assert by_code["4100"] == 200_00
    assert by_code["4200"] == 300_00
    assert cash["totals"]["revenue_cents"] == 500_00

    accrual = reports.pnl_accrual(db, COMPANY, start=date(2026, 7, 1), end=date(2026, 7, 31))
    assert accrual["totals"]["revenue_cents"] == 1000_00


def test_pnl_cash_caps_cumulative_recognition_at_total(db):
    invoice = _invoice(db, lines=[("1000.00", None)], invoice_date=date(2026, 7, 2))
    _pay(db, invoice, amount="600.00", payment_date=date(2026, 7, 5))
    _pay(db, invoice, amount="600.00", payment_date=date(2026, 7, 20))  # overpay

    cash = reports.pnl_cash(db, COMPANY, start=date(2026, 7, 1), end=date(2026, 7, 31))
    # 600 + 400 — the second payment's excess is 2300, never revenue
    assert cash["totals"]["revenue_cents"] == 1000_00


def test_pnl_cash_refund_prorates_negatively_in_its_month(db):
    invoice = _invoice(db, lines=[("1000.00", None)], invoice_date=date(2026, 7, 2))
    _pay(db, invoice, amount="1000.00", payment_date=date(2026, 7, 5))
    refund = InvoiceAdjustment(
        id=uuid4(),
        invoice_id=invoice.id,
        kind="refund",
        amount=Decimal("250.00"),
        reason="warranty",
        refund_method="check",
        company_id=COMPANY,
    )
    db.add(refund)
    db.flush()
    refund.created_at = datetime(2026, 8, 3, 10, 0)
    db.flush()

    july = reports.pnl_cash(db, COMPANY, start=date(2026, 7, 1), end=date(2026, 7, 31))
    assert july["totals"]["revenue_cents"] == 1000_00
    august = reports.pnl_cash(db, COMPANY, start=date(2026, 8, 1), end=date(2026, 8, 31))
    assert august["totals"]["revenue_cents"] == -250_00


def test_pnl_cash_skips_irreconcilable_invoice_instead_of_500(db):
    # The prod population this guards: line-less QB-imported invoices whose
    # residual (total − Σlines − tax) exceeds rounding. One of them with
    # cash activity must skip ITSELF into the payload, not kill the report.
    good = _invoice(db, lines=[("500.00", None)], invoice_date=date(2026, 7, 2))
    _pay(db, good, amount="500.00", payment_date=date(2026, 7, 5))
    bad = _invoice(db, lines=[("10.00", None)], invoice_date=date(2026, 7, 3), issue=False)
    bad.total = Decimal("400.00")  # editable while draft; leaves lines at $10
    bad.balance_due = Decimal("400.00")
    db.flush()
    # issued pre-GL, like the real imported population: flag off → no P1
    settings = ensure_gl_seed(db, COMPANY)
    settings.ledger_posting_enabled = False
    db.flush()
    transition_invoice_status(db, bad, "sent", actor="t")
    settings.ledger_posting_enabled = True
    db.flush()
    payment = Payment(
        id=uuid4(),
        invoice_id=bad.id,
        amount=Decimal("400.00"),
        method="check",
        payment_date=date(2026, 7, 6),
        company_id=COMPANY,
    )
    db.add(payment)  # cash event on the bad invoice, no GL posting needed
    db.flush()

    cash = reports.pnl_cash(db, COMPANY, start=date(2026, 7, 1), end=date(2026, 7, 31))
    assert cash["totals"]["revenue_cents"] == 500_00  # the good invoice only
    assert [row["invoice_number"] for row in cash["skipped_invoices"]] == [bad.invoice_number]
    # CWE-209: the payload reason is exactly the fixed string — exception
    # text (residual cents etc.) goes to the server log, not the response.
    assert cash["skipped_invoices"][0]["reason"] == reports._SKIP_REASON


def test_pnl_cash_credit_applied_recognizes_at_application(db):
    # Overpay invoice A in July (excess never recognized), apply the credit
    # to invoice B in August — the $200 recognizes against B in August.
    a = _invoice(db, lines=[("1000.00", None)], invoice_date=date(2026, 7, 2))
    _pay(db, a, amount="1200.00", payment_date=date(2026, 7, 5))
    b = _invoice(db, lines=[("300.00", None)], invoice_date=date(2026, 7, 20))
    applied = InvoiceAdjustment(
        id=uuid4(),
        invoice_id=b.id,
        kind="credit_applied",
        amount=Decimal("200.00"),
        reason="apply credit",
        company_id=COMPANY,
    )
    db.add(applied)
    db.flush()
    applied.created_at = datetime(2026, 8, 4, 10, 0)
    db.flush()

    july = reports.pnl_cash(db, COMPANY, start=date(2026, 7, 1), end=date(2026, 7, 31))
    assert july["totals"]["revenue_cents"] == 1000_00  # A capped at total
    august = reports.pnl_cash(db, COMPANY, start=date(2026, 8, 1), end=date(2026, 8, 31))
    assert august["totals"]["revenue_cents"] == 200_00  # B via applied credit


def test_pnl_cash_era_invoice_uses_operational_lines(db):
    # A P8-anchored pre-cutover invoice has no P1 — cash derivation still
    # attributes its post-cutover payment from the operational lines (§5.7).
    settings = ensure_gl_seed(db, COMPANY)
    settings.cutover_month = CUTOVER
    db.commit()
    invoice = _invoice(
        db, lines=[("800.00", None)], invoice_date=date(2026, 6, 5), issue=False
    )
    transition_invoice_status(db, invoice, "sent", actor="t")  # era → anchors, no P1
    db.flush()
    _pay(db, invoice, amount="800.00", payment_date=date(2026, 7, 15))

    cash = reports.pnl_cash(db, COMPANY, start=date(2026, 7, 1), end=date(2026, 7, 31))
    assert cash["totals"]["revenue_cents"] == 800_00
    # and the ACCRUAL July P&L shows nothing for it (revenue is QBO-era)
    accrual = reports.pnl_accrual(db, COMPANY, start=date(2026, 7, 1), end=date(2026, 7, 31))
    assert accrual["totals"]["revenue_cents"] == 0


# ---------------------------------------------------------------------------
# Balance sheet
# ---------------------------------------------------------------------------

def test_balance_sheet_re_rollup_balances(db):
    invoice = _invoice(db, lines=[("500.00", None)], tax="30.00")
    _pay(db, invoice, amount="530.00", payment_date=date(2026, 7, 9))
    expense = Expense(
        id=uuid4(),
        vendor="Fuel Co",
        amount=Decimal("60.00"),
        category="fuel",
        date=date(2026, 7, 10),
        company_id=COMPANY,
    )
    db.add(expense)
    db.flush()
    post_expense_recorded(db, expense, actor="t")
    db.flush()

    bs = reports.balance_sheet(db, COMPANY, as_of=date(2026, 7, 31))
    assert bs["totals"]["zero_proof_cents"] == 0
    re_row = next(r for r in bs["equity"] if "Retained Earnings" in r["name"])
    assert re_row["amount_cents"] == 500_00 - 60_00  # revenue − expense
    assert bs["totals"]["asset_cents"] == 530_00 - 60_00
    assert bs["totals"]["liability_cents"] == 30_00


# ---------------------------------------------------------------------------
# Journal browser
# ---------------------------------------------------------------------------

def test_journal_page_drills_to_sources_and_receipts(db):
    invoice = _invoice(db, lines=[("500.00", None)])
    _pay(db, invoice, amount="100.00", payment_date=date(2026, 7, 9))
    expense = Expense(
        id=uuid4(),
        vendor="Fuel Co",
        amount=Decimal("60.00"),
        category="fuel",
        date=date(2026, 7, 10),
        company_id=COMPANY,
    )
    db.add(expense)
    db.flush()
    db.add(
        ExpenseReceipt(
            id=uuid4(),
            expense_id=expense.id,
            filename="pump.jpg",
            content_type="image/jpeg",
            size_bytes=1234,
            sha256="ab" * 32,
            storage_path="x/pump.jpg",
            company_id=COMPANY,
        )
    )
    db.flush()
    post_expense_recorded(db, expense, actor="t")
    db.flush()

    page = reports.journal_page(db, COMPANY)
    assert page["total"] == 3
    by_source = {e["source"]["source_type"]: e for e in page["entries"]}
    assert by_source["invoice"]["source"]["invoice_number"] == invoice.invoice_number
    assert by_source["payment"]["source"]["invoice_id"] == str(invoice.id)
    expense_entry = by_source["expense"]
    assert expense_entry["source"]["vendor"] == "Fuel Co"
    assert expense_entry["source"]["receipts"][0]["filename"] == "pump.jpg"
    # every entry carries balanced lines with account codes
    for entry in page["entries"]:
        assert sum(line["amount_cents"] for line in entry["lines"]) == 0
        assert all(line["account_code"] for line in entry["lines"])

    filtered = reports.journal_page(db, COMPANY, source_type="expense")
    assert filtered["total"] == 1
    paged = reports.journal_page(db, COMPANY, limit=2, offset=2)
    assert paged["total"] == 3
    assert len(paged["entries"]) == 1


# ---------------------------------------------------------------------------
# Endpoint layer (house pattern: called directly)
# ---------------------------------------------------------------------------

def test_endpoints_validate_and_run(db):
    _invoice(db, lines=[("500.00", None)])

    tb = report_trial_balance(as_of="2026-07-31", db=db, user=USER, _perm=None)
    assert tb["totals"]["zero_proof_cents"] == 0

    pnl = report_pnl(start="2026-07-01", end="2026-07-31", basis="cash", db=db, user=USER, _perm=None)
    assert pnl["basis"] == "cash"

    bs = report_balance_sheet(as_of="2026-07-31", db=db, user=USER, _perm=None)
    assert bs["totals"]["zero_proof_cents"] == 0

    journal = journal_browser(db=db, user=USER, _perm=None)
    assert journal["total"] >= 1

    with pytest.raises(HTTPException) as exc:
        report_pnl(start="2026-07-31", end="2026-07-01", basis="accrual", db=db, user=USER, _perm=None)
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException):
        report_pnl(basis="vibes", db=db, user=USER, _perm=None)
    with pytest.raises(HTTPException):
        report_trial_balance(as_of="not-a-date", db=db, user=USER, _perm=None)
    with pytest.raises(HTTPException):
        journal_browser(source_type="martian", db=db, user=USER, _perm=None)
