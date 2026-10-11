"""GDXA-592 — the shared "net billed" helper: an invoice's total less its credit memos.

A credit memo lowers ``balance_due`` and never ``Invoice.total`` (by design,
GDXA-590), so ``SUM(Invoice.total)`` overstates a credited invoice by the whole
credit. The case each test pins is the one in the issue: an invoice of 100 with
a credit memo of 20 counts as 80.

The SQLite arm drives the real ``issue_credit_memo`` / ``record_payment`` /
``apply_customer_credit`` paths. The Postgres arm is the only proof for the
Billing dashboard's ``paid_this_month``: SQLite casts a stored timestamp to
DATE as the number 2026, so that KPI is 0 there whatever the query sums (see
``test_paid_this_month_starts_on_the_shops_month``).
"""
from __future__ import annotations

import secrets
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from gdx_dispatch.core.invoice_net_billed import (
    invoice_net_billed_sq,
    invoice_net_billed_sql,
    net_billed_bulk,
)
from gdx_dispatch.models.tenant_models import AppSettings, Invoice, InvoiceLine
from gdx_dispatch.modules.ledger.service import ensure_gl_seed, transition_invoice_status
from gdx_dispatch.routers.invoices import (
    ApplyCreditIn,
    CreditMemoIn,
    PaymentCreateIn,
    apply_customer_credit,
    billing_summary,
    issue_credit_memo,
    record_payment,
)

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"tenant_id": COMPANY, "sub": "tester"}


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    tenant_db.add(AppSettings(timezone="America/Chicago"))
    ensure_gl_seed(tenant_db, COMPANY)
    tenant_db.commit()
    return tenant_db


def _sent_invoice(db, total="100.00", customer_id=None):
    inv = Invoice(
        id=uuid4(),
        customer_id=customer_id or uuid4(),
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        status="draft",
        subtotal=Decimal(total),
        tax_amount=Decimal("0.00"),
        total=Decimal(total),
        balance_due=Decimal(total),
        invoice_date=date(2026, 9, 1),
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
    transition_invoice_status(db, inv, "sent")
    db.commit()
    return inv


def _credit(db, inv, amount):
    issue_credit_memo(str(inv.id), CreditMemoIn(amount=amount, reason="warranty"), db=db, _=USER)


def _sum_net(db, *where):
    return Decimal(str(db.scalar(
        select(func.coalesce(func.sum(invoice_net_billed_sq()), 0)).where(*where)
    )))


def test_credited_invoice_nets_to_80_in_every_form(db):
    inv = _sent_invoice(db)
    _credit(db, inv, 20.0)
    db.refresh(inv)
    assert inv.total == Decimal("100.00"), "the design stays: a credit never rewrites total"

    assert _sum_net(db, Invoice.id == inv.id) == Decimal("80.00")

    per_customer = dict(db.execute(
        select(Invoice.customer_id, func.sum(invoice_net_billed_sq())).group_by(Invoice.customer_id)
    ).all())
    assert Decimal(str(per_customer[inv.customer_id])) == Decimal("80.00")

    assert net_billed_bulk(db, [inv.id]) == {str(inv.id): Decimal("80.00")}

    raw = db.execute(
        text(f"SELECT SUM({invoice_net_billed_sql('i')}) FROM invoices i WHERE i.deleted_at IS NULL")  # noqa: S608 — fixed alias
    ).scalar_one()
    assert Decimal(str(raw)) == Decimal("80.00")


def test_uncredited_invoices_count_at_total_beside_a_credited_one(db):
    customer = uuid4()
    credited = _sent_invoice(db, customer_id=customer)
    plain = _sent_invoice(db, total="250.00", customer_id=customer)
    _credit(db, credited, 20.0)
    _credit(db, credited, 5.0)  # two memos on one invoice both net

    assert _sum_net(db, Invoice.customer_id == customer) == Decimal("325.00")
    assert net_billed_bulk(db, [credited.id, plain.id, uuid4()]) == {
        str(credited.id): Decimal("75.00"),
        str(plain.id): Decimal("250.00"),
    }


def test_paid_by_credit_memo_plus_payment_counts_what_was_billed(db):
    """The issue's status='paid' arm: 20 credited, 80 paid, closes paid at 80."""
    inv = _sent_invoice(db)
    _credit(db, inv, 20.0)
    record_payment(inv.id, PaymentCreateIn(amount=80.0, method="cash"), _=USER, db=db)
    db.refresh(inv)
    assert inv.status == "paid"
    assert _sum_net(db, Invoice.status == "paid") == Decimal("80.00")


def test_applied_credit_is_settlement_and_does_not_net(db):
    """credit_applied moves a customer credit onto another invoice; that
    invoice was still billed in full (the _credits_by_period rule)."""
    settings = ensure_gl_seed(db, COMPANY)
    settings.ledger_posting_enabled = True  # customer credits live on the ledger
    db.commit()
    customer = uuid4()
    source = _sent_invoice(db, customer_id=customer)
    record_payment(
        source.id, PaymentCreateIn(amount=130.0, method="cash", allow_overpayment=True), _=USER, db=db
    )
    target = _sent_invoice(db, total="50.00", customer_id=customer)
    apply_customer_credit(target.id, ApplyCreditIn(amount=30.0), db=db, _=USER)
    db.refresh(target)
    assert target.balance_due == Decimal("20.00"), "the credit landed as credit_applied"

    assert _sum_net(db, Invoice.id == target.id) == Decimal("50.00")
    assert net_billed_bulk(db, [target.id]) == {str(target.id): Decimal("50.00")}


def test_bulk_with_no_ids_does_no_query(db):
    assert net_billed_bulk(db, []) == {}
    assert net_billed_bulk(db, [None]) == {}


@pytest.mark.parametrize("alias", ["i; DROP TABLE invoices", "i.x", "", "1i"])
def test_raw_sql_alias_must_be_an_identifier(alias):
    with pytest.raises(ValueError):
        invoice_net_billed_sql(alias)


# ── Postgres arm ──────────────────────────────────────────────────────────


def _pg_invoice(session, *, total, status, paid=False):
    inv_id = str(uuid.uuid4())
    session.execute(
        text(
            """
            INSERT INTO invoices (id, invoice_number, status, billing_type,
                                  sequence_number, subtotal, tax_amount, total,
                                  balance_due, locked, public_token,
                                  company_id, created_at, invoice_date, paid_at)
            VALUES (:id, :num, CAST(:status AS invoice_status),
                    CAST('standard' AS invoice_billing_type),
                    1, :total, 0, :total, 0, false, :tok, :company,
                    now(), CURRENT_DATE, CASE WHEN :paid THEN now() END)
            """
        ),
        {"id": inv_id, "num": f"INV-{uuid.uuid4().hex[:6]}", "status": status,
         "total": total, "tok": uuid.uuid4().hex, "company": COMPANY, "paid": paid},
    )
    return inv_id


def _pg_adjustment(session, inv_id, kind, amount):
    session.execute(
        text(
            """
            INSERT INTO invoice_adjustments (id, invoice_id, kind, amount,
                                             tax_component, created_at, company_id)
            VALUES (:id, :inv, CAST(:kind AS invoice_adjustment_kind),
                    :amount, 0, now(), :company)
            """
        ),
        {"id": str(uuid.uuid4()), "inv": inv_id, "kind": kind, "amount": amount,
         "company": COMPANY},
    )


def test_pg_helper_nets_inside_an_aggregate(pg_test_session):
    """The correlated subquery sits inside SUM(); prove Postgres accepts it in
    both the ORM and the raw-SQL form, across a uuid join."""
    credited = _pg_invoice(pg_test_session, total=100, status="sent")
    _pg_adjustment(pg_test_session, credited, "credit_memo", 20)
    _pg_adjustment(pg_test_session, credited, "refund", 7)
    _pg_invoice(pg_test_session, total=40, status="sent")
    pg_test_session.commit()

    orm = pg_test_session.scalar(select(func.sum(invoice_net_billed_sq())))
    raw = pg_test_session.execute(
        text(f"SELECT SUM({invoice_net_billed_sql('i')}) FROM invoices i")  # noqa: S608 — fixed alias
    ).scalar_one()
    assert Decimal(str(orm)) == Decimal("120.00")
    assert Decimal(str(raw)) == Decimal("120.00")


def test_pg_paid_this_month_counts_the_credited_invoice_at_80(pg_test_session):
    """Billing dashboard KPI: 100 invoiced, 20 credited, 80 paid → 80, not 100.
    Fails on the old SUM(Invoice.total)."""
    # tests/fixtures/structure.sql predates migration 055; billing_summary's
    # ready-for-billing count reads the column it added.
    pg_test_session.execute(
        text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS not_billable_at TIMESTAMPTZ")
    )
    inv = _pg_invoice(pg_test_session, total=100, status="paid", paid=True)
    _pg_adjustment(pg_test_session, inv, "credit_memo", 20)
    pg_test_session.commit()

    out = billing_summary(SimpleNamespace(), _=USER, db=pg_test_session)
    assert out["paid_this_month"] == pytest.approx(80.0)
