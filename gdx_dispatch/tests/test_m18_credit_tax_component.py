"""A credit knows the tax it gives back (M18, second half).

Doug's ruling (2026-08-24): pro-rata at the invoice's rate — a credit
reduces tax by amount × (tax/total). invoice_adjustments previously carried
a flat amount with no tax split, so credited tax sat in the sales-tax
report's remittance-liability bucket forever. Now every adjustment writer
stamps tax_component through ONE helper, migration 080 backfills history
with the SAME arithmetic, and the report nets it — credits against the
liability in the credit's own period, refunds against liability and
collected both.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase, ensure_audit_table
from gdx_dispatch.core.invoice_tax import credit_tax_component
from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceAdjustment, InvoiceLine
from gdx_dispatch.routers import reports

TENANT = "tenant-m18"
OFFICE = {"user_id": "office-user", "email": "office@example.com", "role": "admin"}


class _Inv:
    def __init__(self, total, tax):
        self.total, self.tax_amount = total, tax


# ── the one arithmetic ─────────────────────────────────────────────────────

def test_pro_rata_at_the_invoices_rate():
    # the real prod row: $570.79 credit on a $10,110.34 invoice carrying $570.79 tax
    assert credit_tax_component(_Inv("10110.34", "570.79"), "570.79") == Decimal("32.22")


def test_full_credit_gives_back_exactly_the_tax():
    assert credit_tax_component(_Inv("1100.00", "100.00"), "1100.00") == Decimal("100.00")


def test_zero_tax_invoice_yields_zero():
    """MN construction contracts carry no tax — the common case at GDX."""
    assert credit_tax_component(_Inv("500.00", "0"), "250.00") == Decimal("0.00")


def test_component_is_capped_at_the_invoices_tax():
    assert credit_tax_component(_Inv("100.00", "10.00"), "150.00") == Decimal("10.00")


def test_zero_and_negative_amounts_yield_zero():
    assert credit_tax_component(_Inv("100.00", "10.00"), "0") == Decimal("0.00")
    assert credit_tax_component(_Inv("100.00", "10.00"), "-5") == Decimal("0.00")


# ── the report netting (pure rollup — no PG needed) ────────────────────────

def _tax_row(period, source, total, collected, n=1):
    return {"period_start": period, "source": source, "tax_total": total,
            "tax_collected": collected, "invoice_count": n}


def test_rollup_nets_credited_tax_out_of_the_liability():
    rows = [
        _tax_row("2026-07-01", "gdx", 100.0, 100.0),
        {"period_start": "2026-07-01", "row_kind": "adjustments",
         "tax_credited": 30.0, "tax_refunded": 0.0},
    ]
    items, totals = reports._rollup_sales_tax(rows)
    assert totals["tax_total"] == 70.0
    assert totals["tax_collected"] == 100.0
    assert totals["tax_credited"] == 30.0
    assert items[0]["tax_outstanding"] == -30.0  # collected above net liability — visible, not hidden


def test_rollup_nets_refunds_from_both_sides():
    rows = [
        _tax_row("2026-07-01", "gdx", 100.0, 100.0),
        {"period_start": "2026-07-01", "row_kind": "adjustments",
         "tax_credited": 0.0, "tax_refunded": 25.0},
    ]
    items, totals = reports._rollup_sales_tax(rows)
    assert totals["tax_total"] == 75.0
    assert totals["tax_collected"] == 75.0
    assert items[0]["tax_outstanding"] == 0.0


def test_rollup_credit_in_a_period_with_no_billing_still_appears():
    rows = [
        {"period_start": "2026-08-01", "row_kind": "adjustments",
         "tax_credited": 12.5, "tax_refunded": 0.0},
    ]
    items, totals = reports._rollup_sales_tax(rows)
    assert len(items) == 1
    assert items[0]["tax_total"] == -12.5


# ── the writer stamps it (real endpoint, real rows) ────────────────────────

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


def _invoice(db, *, total="1100.00", tax="100.00"):
    cust = Customer(id=uuid.uuid4(), name="M18 Customer", company_id=TENANT)
    db.add(cust)
    inv = Invoice(
        id=uuid.uuid4(), customer_id=cust.id,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}", billing_type="standard",
        sequence_number=1, subtotal=Decimal(total) - Decimal(tax),
        tax_amount=Decimal(tax), total=Decimal(total), balance_due=Decimal(total),
        status="sent", public_token=uuid.uuid4().hex, company_id=TENANT,
    )
    db.add(inv)
    # A real line — _recalculate_invoice re-derives the header from lines
    # before the balance cap, and a line-less invoice collapses to tax-only.
    db.add(InvoiceLine(
        id=uuid.uuid4(), invoice_id=inv.id, description="Door install",
        quantity=1, unit_price=Decimal(total) - Decimal(tax),
        line_total=Decimal(total) - Decimal(tax), taxable=True,
        sort_order=1, company_id=TENANT,
    ))
    db.commit()
    return inv


def test_issue_credit_memo_stamps_the_tax_component(db):
    from gdx_dispatch.routers.invoices import CreditMemoIn, issue_credit_memo

    inv = _invoice(db)  # 1100 total, 100 tax
    issue_credit_memo(str(inv.id), CreditMemoIn(amount=550.00, reason="half forgiven"), db=db, _=OFFICE)
    adj = db.query(InvoiceAdjustment).filter(InvoiceAdjustment.kind == "credit_memo").one()
    assert Decimal(str(adj.tax_component)) == Decimal("50.00"), (
        "half the invoice credited must carry half its tax"
    )


# ── the backfill uses the SAME arithmetic (SQLite lane of migration 080) ───

def test_backfill_sql_matches_the_helper(db):
    inv = _invoice(db, total="10110.34", tax="570.79")
    adj = InvoiceAdjustment(
        invoice_id=inv.id, kind="credit_memo", amount=Decimal("570.79"),
        tax_component=Decimal("0.00"), reason="historic", company_id=TENANT,
        created_at=datetime.now(UTC),
    )
    db.add(adj)
    db.commit()
    import importlib
    mig = importlib.import_module("gdx_dispatch.migrations.versions.080_adjustment_tax_component")
    db.execute(text(mig._BACKFILL_SQLITE))
    db.commit()
    db.refresh(adj)
    assert Decimal(str(adj.tax_component)) == credit_tax_component(inv, adj.amount) == Decimal("32.22")


# ── Postgres lane: the real report SQL + the PG backfill ───────────────────
# (pg_test_session skips without a reachable PG — runs in the CI shards and
#  under --network host locally, per the harness convention.)

def test_pg_report_nets_credited_tax(pg_test_session):
    """End-to-end on the real date_trunc SQL: an invoice with $100 tax and a
    credit carrying a $30 tax_component → liability 70, collected untouched."""
    import pathlib  # noqa: F401  (parity with the sibling PG tests' imports)
    from datetime import timedelta

    from gdx_dispatch.tests.test_reports import PG_COMPANY, _pg_seed_job

    job = _pg_seed_job(pg_test_session)
    when = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    inv_id = str(uuid.uuid4())
    pg_test_session.execute(
        text(
            """
            INSERT INTO invoices (id, job_id, invoice_number, status, billing_type,
                                  sequence_number, subtotal, tax_amount, total,
                                  balance_due, locked, public_token,
                                  company_id, created_at, invoice_date)
            VALUES (:id, :job_id, :num, CAST('sent' AS invoice_status),
                    CAST('standard' AS invoice_billing_type),
                    1, 1000, 100, 1100, 1100, false, :tok, :company,
                    now(), CAST(:when AS DATE))
            """
        ),
        {"id": inv_id, "job_id": job, "num": f"INV-{uuid.uuid4().hex[:6]}",
         "tok": uuid.uuid4().hex, "company": PG_COMPANY, "when": when},
    )
    pg_test_session.execute(
        text(
            """
            INSERT INTO invoice_adjustments (id, invoice_id, kind, amount,
                                             tax_component, created_at, company_id)
            VALUES (:id, :inv, CAST('credit_memo' AS invoice_adjustment_kind),
                    330.00, 30.00, now(), :company)
            """
        ),
        {"id": str(uuid.uuid4()), "inv": inv_id, "company": PG_COMPANY},
    )
    pg_test_session.commit()

    from datetime import timedelta as _td
    start = (datetime.now(UTC) - _td(days=10)).date().isoformat()
    end = (datetime.now(UTC) + _td(days=1)).date().isoformat()
    data = reports.sales_tax_report(start, end, "month", {}, pg_test_session)
    assert data["totals"]["tax_credited"] == pytest.approx(30.0)
    assert data["totals"]["tax_total"] == pytest.approx(70.0), (
        "credited tax must leave the remittance liability"
    )


def test_pg_backfill_matches_the_helper(pg_test_session):
    """Migration 080's PG UPDATE…FROM lane, against the prod-row arithmetic."""
    from datetime import timedelta

    from gdx_dispatch.tests.test_reports import PG_COMPANY, _pg_seed_job

    job = _pg_seed_job(pg_test_session)
    inv_id = str(uuid.uuid4())
    when = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    pg_test_session.execute(
        text(
            """
            INSERT INTO invoices (id, job_id, invoice_number, status, billing_type,
                                  sequence_number, subtotal, tax_amount, total,
                                  balance_due, locked, public_token,
                                  company_id, created_at, invoice_date)
            VALUES (:id, :job_id, :num, CAST('paid' AS invoice_status),
                    CAST('standard' AS invoice_billing_type),
                    1, 9539.55, 570.79, 10110.34, 0, false, :tok, :company,
                    now(), CAST(:when AS DATE))
            """
        ),
        {"id": inv_id, "job_id": job, "num": "49796251-t",
         "tok": uuid.uuid4().hex, "company": PG_COMPANY, "when": when},
    )
    adj_id = str(uuid.uuid4())
    pg_test_session.execute(
        text(
            """
            INSERT INTO invoice_adjustments (id, invoice_id, kind, amount,
                                             tax_component, created_at, company_id)
            VALUES (:id, :inv, CAST('credit_memo' AS invoice_adjustment_kind),
                    570.79, 0.00, now(), :company)
            """
        ),
        {"id": adj_id, "inv": inv_id, "company": PG_COMPANY},
    )
    pg_test_session.commit()
    import importlib
    mig = importlib.import_module("gdx_dispatch.migrations.versions.080_adjustment_tax_component")
    pg_test_session.execute(text(mig._BACKFILL_PG))
    pg_test_session.commit()
    got = pg_test_session.execute(
        text("SELECT tax_component FROM invoice_adjustments WHERE id = :i"), {"i": adj_id}
    ).scalar()
    assert Decimal(str(got)) == Decimal("32.22")


def test_backfill_does_not_integer_divide_whole_dollar_rows(db):
    """Audit round 2 reproduced it: SQLite NUMERIC affinity stores 500/100/1100
    as INTEGERs and 500*100/1100 integer-divides to 45 (vs 45.45). The *1.0
    coercion is load-bearing."""
    inv = _invoice(db, total="1100", tax="100")  # integer-affinity storage
    adj = InvoiceAdjustment(
        invoice_id=inv.id, kind="credit_memo", amount=Decimal("500"),
        tax_component=Decimal("0.00"), reason="historic", company_id=TENANT,
        created_at=datetime.now(UTC),
    )
    db.add(adj)
    db.commit()
    import importlib
    mig = importlib.import_module("gdx_dispatch.migrations.versions.080_adjustment_tax_component")
    db.execute(text(mig._BACKFILL_SQLITE))
    db.commit()
    db.refresh(adj)
    assert Decimal(str(adj.tax_component)) == Decimal("45.45"), (
        f"integer division regressed: {adj.tax_component}"
    )


def test_pg_a_voided_invoices_credit_does_not_net(pg_test_session):
    """Round 2's foundational hole: void is allowed on a credited invoice,
    the GL entries reverse but the adjustment ROW survives — its component
    must stop netting the moment the parent leaves the liability universe."""
    from datetime import timedelta

    from gdx_dispatch.tests.test_reports import PG_COMPANY, _pg_seed_job

    job = _pg_seed_job(pg_test_session)
    when = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    inv_id = str(uuid.uuid4())
    pg_test_session.execute(
        text(
            """
            INSERT INTO invoices (id, job_id, invoice_number, status, billing_type,
                                  sequence_number, subtotal, tax_amount, total,
                                  balance_due, locked, public_token,
                                  company_id, created_at, invoice_date)
            VALUES (:id, :job_id, :num, CAST('void' AS invoice_status),
                    CAST('standard' AS invoice_billing_type),
                    1, 1000, 100, 1100, 0, false, :tok, :company,
                    now(), CAST(:when AS DATE))
            """
        ),
        {"id": inv_id, "job_id": job, "num": f"INV-{uuid.uuid4().hex[:6]}",
         "tok": uuid.uuid4().hex, "company": PG_COMPANY, "when": when},
    )
    pg_test_session.execute(
        text(
            """
            INSERT INTO invoice_adjustments (id, invoice_id, kind, amount,
                                             tax_component, created_at, company_id)
            VALUES (:id, :inv, CAST('credit_memo' AS invoice_adjustment_kind),
                    330.00, 30.00, now(), :company)
            """
        ),
        {"id": str(uuid.uuid4()), "inv": inv_id, "company": PG_COMPANY},
    )
    pg_test_session.commit()
    from datetime import timedelta as _td
    start = (datetime.now(UTC) - _td(days=10)).date().isoformat()
    end = (datetime.now(UTC) + _td(days=1)).date().isoformat()
    data = reports.sales_tax_report(start, end, "month", {}, pg_test_session)
    assert data["totals"]["tax_credited"] == pytest.approx(0.0), (
        "a voided invoice's tax never entered the liability — its credit must not leave it"
    )
