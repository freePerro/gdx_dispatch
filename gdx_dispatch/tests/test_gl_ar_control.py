"""GDXA-1181: the AR control tie (gl-phase3-trust-switch §3.2 criterion 3).

Open-invoice balances (each live invoice's ``balance_due``) must equal the
net of the AR control account, and every AR line must trace to an invoice.
Each test drifts the books one way the trial-balance zero-check cannot see,
through the real posting rules and a real SQLite session, and reads the
report back.

Not caught, by construction: a payment recorded on the wrong invoice whose
posting followed it. The row and the ledger agree, so only the customer
(through a statement) can tell.
"""
from __future__ import annotations

import secrets
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from gdx_dispatch.models.tenant_models import Invoice, InvoiceAdjustment, InvoiceLine, Payment
from gdx_dispatch.modules.ledger import reports
from gdx_dispatch.modules.ledger.engine import PostingEvent, PostingLine, post_for_event
from gdx_dispatch.modules.ledger.models import ROLE_AR, ROLE_OPENING_EQUITY
from gdx_dispatch.modules.ledger.router import report_ar_control
from gdx_dispatch.modules.ledger.rules import post_credit_memo, post_payment_received
from gdx_dispatch.modules.ledger.service import ensure_gl_seed, transition_invoice_status
from gdx_dispatch.routers.invoices import _recalculate_invoice

COMPANY = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    settings = ensure_gl_seed(tenant_db, COMPANY)
    settings.ledger_posting_enabled = True
    tenant_db.commit()
    return tenant_db


def _issued(
    db, amount: str, *, status: str = "sent", invoice_date: date = date(2026, 7, 8)
) -> Invoice:
    """A one-line invoice moved to ``status`` through the chokepoint."""
    total = Decimal(amount)
    inv = Invoice(
        id=uuid4(),
        customer_id=uuid4(),
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status="draft",
        subtotal=total,
        tax_amount=Decimal("0"),
        total=total,
        balance_due=total,
        invoice_date=invoice_date,
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=COMPANY,
    )
    db.add(inv)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=inv.id, description="repair", quantity=1,
            unit_price=total, line_total=total, company_id=COMPANY,
        )
    )
    db.flush()
    if status != "draft":
        transition_invoice_status(db, inv, status, actor="t")
        db.flush()
    return inv


def _payment(
    db, *, recorded_on: Invoice, posted_against: Invoice, amount: str,
    paid_on: date = date(2026, 7, 9),
) -> Payment:
    payment = Payment(
        id=uuid4(), invoice_id=recorded_on.id, amount=Decimal(amount),
        method="check", payment_date=paid_on, company_id=COMPANY,
    )
    db.add(payment)
    db.flush()
    post_payment_received(db, payment, posted_against, actor="t")
    db.flush()
    return payment


def test_ties_when_every_balance_matches_the_ledger(db):
    inv = _issued(db, "530.00")
    _payment(db, recorded_on=inv, posted_against=inv, amount="200.00")
    inv.balance_due = Decimal("330.00")
    db.flush()

    out = reports.ar_control(db, COMPANY)
    assert out["control_cents"] == 330_00
    assert out["subledger_cents"] == 330_00
    assert out["difference_cents"] == 0
    assert out["drift"] == []
    assert out["tied"] is True


def test_the_real_recalculation_ties_after_a_credit_memo_and_an_overpayment(db):
    # balance_due computed by the production recalc, not set by the test.
    inv = _issued(db, "100.00")
    memo = InvoiceAdjustment(
        id=uuid4(), invoice_id=inv.id, kind="credit_memo", amount=Decimal("20.00"),
        reason="goodwill", created_by="t", company_id=COMPANY,
    )
    db.add(memo)
    db.flush()
    post_credit_memo(db, memo, inv, actor="t")
    _payment(db, recorded_on=inv, posted_against=inv, amount="30.00")
    _recalculate_invoice(inv, db, actor="t")
    db.flush()
    assert inv.balance_due == Decimal("50.00")
    assert reports.ar_control(db, COMPANY)["tied"] is True

    # $90 on a $50 balance: $40 is customer credit (2300), not negative AR.
    _payment(db, recorded_on=inv, posted_against=inv, amount="90.00")
    _recalculate_invoice(inv, db, actor="t")
    db.flush()
    out = reports.ar_control(db, COMPANY)
    assert inv.balance_due == Decimal("0.00")
    assert (out["control_cents"], out["tied"]) == (0, True)


def test_a_pre_cutover_invoice_ties_through_its_opening_anchor(db):
    settings = ensure_gl_seed(db, COMPANY)
    settings.cutover_month = date(2026, 7, 1)
    db.flush()
    era = _issued(db, "400.00", invoice_date=date(2026, 6, 15))
    _payment(db, recorded_on=era, posted_against=era, amount="150.00", paid_on=date(2026, 7, 20))
    _recalculate_invoice(era, db, actor="t")
    db.flush()

    out = reports.ar_control(db, COMPANY)
    assert era.balance_due == Decimal("250.00")
    assert (out["control_cents"], out["subledger_cents"], out["tied"]) == (250_00, 250_00, True)


def test_balance_edited_without_a_repost_is_named(db):
    inv = _issued(db, "530.00")
    inv.balance_due = Decimal("600.00")  # the row moved, the ledger did not
    db.flush()

    out = reports.ar_control(db, COMPANY)
    assert out["tied"] is False
    assert out["difference_cents"] == -70_00
    [row] = out["drift"]
    assert row["invoice_number"] == inv.invoice_number
    assert (row["open_balance_cents"], row["ledger_ar_cents"], row["drift_cents"]) == (
        600_00, 530_00, -70_00,
    )


def test_an_issued_invoice_that_never_posted_is_named(db):
    _issued(db, "100.00")
    # Issued while posting was off and never backfilled: the row is sent,
    # the ledger never heard of it.
    settings = ensure_gl_seed(db, COMPANY)
    settings.ledger_posting_enabled = False
    db.flush()
    orphan = _issued(db, "250.00")
    settings.ledger_posting_enabled = True
    db.flush()

    out = reports.ar_control(db, COMPANY)
    assert out["control_cents"] == 100_00
    assert out["subledger_cents"] == 350_00
    [row] = out["drift"]
    assert row["invoice_id"] == str(orphan.id)
    assert row["ledger_ar_cents"] == 0


def test_offsetting_drift_on_two_invoices_nets_to_zero_and_still_fails(db):
    a = _issued(db, "300.00")
    b = _issued(db, "300.00")
    pay = _payment(db, recorded_on=a, posted_against=a, amount="100.00")
    # The office's balances say the $100 came off B, but the only posting that
    # relieved AR is sourced from A's payment: a balance moved between
    # invoices with no entry behind it.
    a.balance_due = Decimal("300.00")
    b.balance_due = Decimal("200.00")
    db.flush()

    out = reports.ar_control(db, COMPANY)
    # Attribution follows the payment row: A's ledger relief is A's, so A
    # owes 300 on the row but 200 on the ledger, and B the reverse. The
    # totals agree; only the per-invoice walk sees it.
    assert pay.invoice_id == a.id
    assert out["difference_cents"] == 0
    assert out["tied"] is False
    by_id = {r["invoice_id"]: r["drift_cents"] for r in out["drift"]}
    assert by_id == {str(a.id): -100_00, str(b.id): 100_00}


def test_ar_that_no_invoice_claims_is_reported_by_source(db):
    _issued(db, "100.00")
    post_for_event(
        db,
        PostingEvent(
            company_id=COMPANY, source_type="manual", source_id=str(uuid4()),
            event="adjust", effective_at=date(2026, 7, 10),
            lines=(
                PostingLine(amount_cents=45_00, role=ROLE_AR, memo="hand entry"),
                PostingLine(amount_cents=-45_00, role=ROLE_OPENING_EQUITY, memo="hand entry"),
            ),
            created_by="t",
        ),
    )
    db.flush()

    out = reports.ar_control(db, COMPANY)
    assert out["drift"] == []
    assert out["unattributed_cents"] == 45_00
    assert out["unattributed_by_source"] == [{"source_type": "manual", "amount_cents": 45_00}]
    assert out["difference_cents"] == 45_00
    assert out["tied"] is False


def test_void_and_draft_owe_nothing(db):
    _issued(db, "100.00")
    voided = _issued(db, "70.00")
    transition_invoice_status(db, voided, "void", actor="t")
    voided.balance_due = Decimal("0")
    _issued(db, "80.00", status="draft")

    out = reports.ar_control(db, COMPANY)
    assert out["subledger_cents"] == 100_00
    assert out["control_cents"] == 100_00
    assert out["tied"] is True


def test_endpoint_returns_the_report(db):
    _issued(db, "100.00")
    out = report_ar_control(db=db, user={"tenant_id": COMPANY, "sub": "tester"}, _perm=None)
    assert out["ledger_posting_enabled"] is True
    assert out["control_cents"] == 100_00
    assert out["tied"] is True
