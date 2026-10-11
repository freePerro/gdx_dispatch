"""GDXA-594 — job costing and job profitability count a credited invoice at net.

A credit memo lowers ``balance_due`` and never ``Invoice.total`` (by design,
GDXA-590), so every per-job revenue figure that summed ``Invoice.total``
overstated a credited job by the whole credit. The case is the issue's: an
invoice of 100 with a credit memo of 20 reads 80 on every jobs-dispatch
surface, and a void invoice on the same job adds nothing.

The credit goes through the real ``issue_credit_memo``; the four surfaces are
called as handlers on the same tenant session.
"""
from __future__ import annotations

import json
import secrets
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from gdx_dispatch.models.tenant_models import AppSettings, Invoice, InvoiceLine, Job
from gdx_dispatch.modules.ledger.service import ensure_gl_seed, transition_invoice_status
from gdx_dispatch.routers import job_costing, jobs
from gdx_dispatch.routers.invoices import CreditMemoIn, issue_credit_memo

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"tenant_id": COMPANY, "sub": "tester"}
REQUEST = SimpleNamespace(state=SimpleNamespace(tenant={"id": COMPANY}))


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    tenant_db.add(AppSettings(timezone="America/Chicago"))
    ensure_gl_seed(tenant_db, COMPANY)
    tenant_db.commit()
    return tenant_db


def _job(db):
    job = Job(id=uuid4(), title="Spring replacement", customer_id=uuid4(), company_id=COMPANY)
    db.add(job)
    db.commit()
    return job


def _sent_invoice(db, job, total="100.00"):
    inv = Invoice(
        id=uuid4(),
        customer_id=job.customer_id,
        job_id=job.id,
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


@pytest.fixture
def credited_job(db):
    """A job with a 100 invoice credited 20, and a void 500 invoice beside it."""
    job = _job(db)
    inv = _sent_invoice(db, job)
    issue_credit_memo(str(inv.id), CreditMemoIn(amount=20.0, reason="warranty"), db=db, _=USER)
    void = _sent_invoice(db, job, total="500.00")
    transition_invoice_status(db, void, "void")
    db.commit()
    db.refresh(inv)
    assert inv.total == Decimal("100.00"), "the design stays: a credit never rewrites total"
    return job


def test_job_costing_invoiced_is_net_of_the_credit(db, credited_job):
    assert job_costing._invoiced_for_job(db, credited_job.id, COMPANY) == Decimal("80.00")


def test_profitability_report_invoice_total_is_net_of_the_credit(db, credited_job):
    rows = job_costing.profitability_report(REQUEST, {}, db, days=3650)
    row = next(r for r in rows if r["job_id"] == str(credited_job.id))
    assert row["invoice_total"] == 80.0


def test_job_profitability_revenue_is_net_of_the_credit(db, credited_job):
    resp = jobs.get_job_costing(str(credited_job.id), REQUEST, USER, db)
    body = json.loads(resp.body)
    assert body["total_revenue"] == 80.0
    assert body["profit"] == 80.0 - body["total_cost"]


def test_job_financials_invoiced_is_net_but_each_row_keeps_its_issued_total(db, credited_job):
    body = jobs.get_job_financials(str(credited_job.id), USER, db)
    assert body["invoiced_total"] == 80.0
    assert sorted(r["total"] for r in body["invoices"]) == [100.0, 500.0]


def test_an_uncredited_job_still_counts_its_whole_total(db):
    job = _job(db)
    _sent_invoice(db, job, total="250.00")
    assert job_costing._invoiced_for_job(db, job.id, COMPANY) == Decimal("250.00")
    assert jobs.get_job_financials(str(job.id), USER, db)["invoiced_total"] == 250.0
