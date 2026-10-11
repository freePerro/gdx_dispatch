"""GDXA-593 — the Reports aggregates count a credited invoice net of its credit memos.

A credit memo lowers ``balance_due`` and never ``Invoice.total`` (by design,
GDXA-590), so every report that summed ``Invoice.total`` as revenue or billed
overstated a credited invoice by the whole credit. The one case pinned here is
the issue's: an invoice of 100 with a credit memo of 20 shows 80 on each
report. The credit goes through the real ``issue_credit_memo`` path, so the
adjustment row is the one production writes.

The cash calendar is deliberately NOT in this file: it keeps the gross total
(see ``_unbilled_value`` in modules/forecasting/cash_calendar.py).
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from freezegun import freeze_time
from sqlalchemy import text

from gdx_dispatch.models.tenant_models import (
    AppSettings,
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    Technician,
)
from gdx_dispatch.modules.ledger.service import ensure_gl_seed, transition_invoice_status
from gdx_dispatch.modules.reporting.service import job_costing_report, revenue_report
from gdx_dispatch.routers import reports
from gdx_dispatch.routers.invoices import CreditMemoIn, issue_credit_memo, void_invoice

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"tenant_id": COMPANY, "sub": "tester"}
DAY = "2026-09-14"
NOON = datetime(2026, 9, 14, 17, 0, tzinfo=UTC)  # noon Central


@pytest.fixture
def credited(tenant_db, monkeypatch):
    """One completed job for one customer and tech, invoiced 100, credited 20,
    the rest paid — all on the shop's 2026-09-14."""
    monkeypatch.delenv("GDX_ENV", raising=False)
    db = tenant_db
    with freeze_time(NOON):
        db.add(AppSettings(timezone="America/Chicago"))
        ensure_gl_seed(db, COMPANY)
        customer = Customer(id=uuid4(), name="Credited Customer", company_id=COMPANY)
        tech = Technician(id=str(uuid4()), name="Tess", company_id=COMPANY)
        db.add_all([customer, tech])
        db.flush()
        job = Job(
            id=uuid4(), customer_id=customer.id, title="Spring swap", job_type="repair",
            lifecycle_stage="completed", completed_at=NOON, assigned_to=tech.id,
            company_id=COMPANY,
        )
        db.add(job)
        db.flush()
        inv = Invoice(
            id=uuid4(), customer_id=customer.id, job_id=job.id,
            invoice_number=f"INV-{uuid4().hex[:8].upper()}", status="draft",
            subtotal=Decimal("100.00"), tax_amount=Decimal("0.00"), total=Decimal("100.00"),
            balance_due=Decimal("100.00"), invoice_date=date(2026, 9, 14),
            public_token=secrets.token_urlsafe(48)[:64], company_id=COMPANY,
        )
        db.add(inv)
        db.flush()
        db.add(InvoiceLine(
            invoice_id=inv.id, description="Work", quantity=1,
            unit_price=Decimal("100.00"), line_total=Decimal("100.00"), company_id=COMPANY,
        ))
        db.commit()
        db.refresh(inv)
        transition_invoice_status(db, inv, "sent")
        db.commit()
        issue_credit_memo(str(inv.id), CreditMemoIn(amount=20.0, reason="warranty"), db=db, _=USER)
        db.refresh(inv)
        inv.status = "paid"
        inv.paid_at = NOON
        db.commit()
    # The windows compare created_at as text on SQLite against ISO bounds
    # ("…T05:00:00+00:00"), so stamp the rows the way the GDXA-538 report
    # tests do. SQLite keeps a Uuid as 32 dashless hex.
    for table, row_id in (("jobs", job.id), ("invoices", inv.id)):
        db.execute(text(f"UPDATE {table} SET created_at = :at WHERE id = :id"),  # noqa: S608 — fixed table names
                   {"at": NOON.isoformat(), "id": row_id.hex})
    db.execute(text("UPDATE invoice_adjustments SET created_at = :at WHERE invoice_id = :id"),
               {"at": NOON.isoformat(), "id": inv.id.hex})
    db.commit()
    db.info.pop("gdx_shop_timezone", None)
    assert inv.total == Decimal("100.00"), "the design stays: a credit never rewrites total"
    assert inv.balance_due == Decimal("80.00")
    return {"db": db, "job": job, "customer": customer, "tech": tech, "invoice": inv}


def _window():
    return {"start_date": DAY, "end_date": DAY, "_": {}}


def test_job_profitability_nets_the_credit(credited):
    items = reports.job_profitability(**_window(), db=credited["db"])["items"]
    assert [(i["job_id"], i["revenue"]) for i in items] == [(str(credited["job"].id), pytest.approx(80.0))]


def test_technician_performance_nets_the_credit(credited):
    items = reports.technician_performance(**_window(), db=credited["db"])["items"]
    assert len(items) == 1
    assert items[0]["revenue"] == pytest.approx(80.0)


def test_revenue_analytics_period_and_job_type_agree_at_80(credited):
    out = reports.revenue_analytics(**_window(), db=credited["db"])
    assert out["total_revenue"] == pytest.approx(80.0)
    assert [r["revenue"] for r in out["by_period"]] == [pytest.approx(80.0)]
    assert out["by_job_type"] == [{"job_type": "repair", "revenue": pytest.approx(80.0)}]


def test_customer_ltv_nets_the_credit(credited):
    items = reports.customer_ltv(**_window(), db=credited["db"])["items"]
    assert [i["lifetime_value"] for i in items] == [pytest.approx(80.0)]


def test_top_customers_nets_the_credit_and_keeps_the_billed_average(credited):
    items = reports.top_customers(**_window(), limit=10, db=credited["db"])["items"]
    assert len(items) == 1
    assert items[0]["total_revenue"] == pytest.approx(80.0)
    assert items[0]["avg_invoice"] == pytest.approx(100.0)


def test_daily_snapshot_matches_summary(credited):
    db = credited["db"]
    snap = reports.daily_snapshot(**_window(), db=db)
    summary = reports.reports_summary(**_window(), db=db)
    assert snap["today_revenue"] == pytest.approx(80.0)
    assert summary["revenue_total"] == pytest.approx(80.0)


def test_voided_credited_invoice_leaves_no_negative_revenue(credited):
    """A void reverses the credit's GL entries but keeps its adjustment row;
    the windowed totals must drop the credit with the invoice, not read -20."""
    db = credited["db"]
    with freeze_time(NOON):
        void_invoice(credited["invoice"].id, _=USER, db=db)
    db.refresh(credited["invoice"])
    assert credited["invoice"].status == "void"

    assert reports.daily_snapshot(**_window(), db=db)["today_revenue"] == pytest.approx(0.0)
    assert reports.reports_summary(**_window(), db=db)["revenue_total"] == pytest.approx(0.0)
    with freeze_time(NOON):
        funnel = reports.sales_funnel(_={}, db=db)
    assert funnel["billed"]["today"] == {"revenue": pytest.approx(0.0), "count": 0}


def test_sales_funnel_billed_nets_the_credit(credited):
    with freeze_time(NOON):
        out = reports.sales_funnel(_={}, db=credited["db"])
    assert out["billed"]["today"] == {"revenue": pytest.approx(80.0), "count": 1}
    assert out["billed"]["this_week"] == {"revenue": pytest.approx(80.0), "count": 1}


def test_revenue_export_nets_the_credit(credited):
    resp = reports.export_report(**_window(), report_type="revenue", db=credited["db"])

    async def _body():
        return "".join([c if isinstance(c, str) else c.decode() async for c in resp.body_iterator])

    lines = asyncio.run(_body()).strip().splitlines()
    assert lines[0].split(",") == ["date", "invoice_count", "revenue"]
    assert len(lines) == 2
    assert Decimal(lines[1].split(",")[2]) == Decimal("80")


def test_reporting_service_nets_the_credit(credited):
    db = credited["db"]
    rows = job_costing_report(date(2026, 9, 14), date(2026, 9, 14), db)
    assert [r["total_billed"] for r in rows] == [pytest.approx(80.0)]
    paid = revenue_report(date(2026, 9, 14), date(2026, 9, 14), db)
    assert paid["total_paid"] == pytest.approx(80.0)
    assert paid["invoice_count"] == 1
    assert paid["avg_invoice"] == pytest.approx(100.0)


# ── Postgres arm ──────────────────────────────────────────────────────────
# The correlated net-billed subquery sits inside SUM() under GROUP BY, HAVING
# and ORDER BY, across outer joins; only Postgres proves it is accepted there.

PG_AT = "2026-09-14T18:00:00+00:00"  # mid-day in UTC and in Central


def _pg(session, sql, **params):
    session.execute(text(sql), params)


def _pg_seed(session):
    customer, job, inv = str(uuid4()), str(uuid4()), str(uuid4())
    tech = str(uuid4())
    _pg(session, "INSERT INTO customers (id, name, company_id) VALUES (:id, 'PG Customer', :co)",
        id=customer, co=COMPANY)
    _pg(session, "INSERT INTO technicians (id, name, company_id) VALUES (:id, 'Tess', :co)",
        id=tech, co=COMPANY)
    _pg(session,
        """INSERT INTO jobs (id, customer_id, title, job_type, assigned_to, lifecycle_stage,
                             dispatch_status, company_id, created_at, completed_at)
           VALUES (:id, :cust, 'Spring swap', 'repair', :tech,
                   CAST('completed' AS job_lifecycle_stage),
                   CAST('unassigned' AS job_dispatch_status), :co, :at, :at)""",
        id=job, cust=customer, tech=tech, co=COMPANY, at=PG_AT)
    _pg(session,
        """INSERT INTO invoices (id, customer_id, job_id, invoice_number, status, billing_type,
                                 sequence_number, subtotal, tax_amount, total, balance_due,
                                 locked, public_token, company_id, created_at, invoice_date)
           VALUES (:id, :cust, :job, 'INV-PG593', CAST('sent' AS invoice_status),
                   CAST('standard' AS invoice_billing_type), 1, 100, 0, 100, 80, false,
                   :tok, :co, :at, DATE '2026-09-14')""",
        id=inv, cust=customer, job=job, tok=uuid4().hex, co=COMPANY, at=PG_AT)
    _pg(session,
        """INSERT INTO invoice_adjustments (id, invoice_id, kind, amount, tax_component,
                                            created_at, company_id)
           VALUES (:id, :inv, CAST('credit_memo' AS invoice_adjustment_kind), 20, 0, :at, :co)""",
        id=str(uuid4()), inv=inv, at=PG_AT, co=COMPANY)
    session.commit()
    return job


def test_pg_grouped_reports_net_the_credit(pg_test_session):
    job = _pg_seed(pg_test_session)
    w = {"start_date": DAY, "end_date": DAY, "_": {}, "db": pg_test_session}

    assert [(i["job_id"], i["revenue"]) for i in reports.job_profitability(**w)["items"]] == [
        (job, pytest.approx(80.0))
    ]
    assert [i["revenue"] for i in reports.technician_performance(**w)["items"]] == [pytest.approx(80.0)]
    analytics = reports.revenue_analytics(**w)
    assert analytics["total_revenue"] == pytest.approx(80.0)
    assert [r["revenue"] for r in analytics["by_job_type"]] == [pytest.approx(80.0)]
    assert [i["lifetime_value"] for i in reports.customer_ltv(**w)["items"]] == [pytest.approx(80.0)]
    top = reports.top_customers(**w, limit=10)["items"]
    assert [(i["total_revenue"], i["avg_invoice"]) for i in top] == [(pytest.approx(80.0), pytest.approx(100.0))]
    assert reports.daily_snapshot(**w)["today_revenue"] == pytest.approx(80.0)
