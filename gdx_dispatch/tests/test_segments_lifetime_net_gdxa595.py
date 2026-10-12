"""GDXA-595 — segment lifetime_value through the real credit-memo path.

``test_segments.py`` drives ``_customer_stats`` over a hand-written schema with
dashed string ids. This file builds the schema from the ORM, issues the credit
with the real ``issue_credit_memo`` and reads the rollup back, so the raw-SQL
join of ``invoice_adjustments.invoice_id`` to ``invoices.id`` is proven on the
storage SQLite really uses for ``Uuid`` (32 dashless hex).
"""
from __future__ import annotations

import secrets
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from gdx_dispatch.models.tenant_models import AppSettings, Customer, Invoice, InvoiceLine, Job
from gdx_dispatch.modules.ledger.service import ensure_gl_seed, transition_invoice_status
from gdx_dispatch.routers.invoices import CreditMemoIn, issue_credit_memo
from gdx_dispatch.routers.segments import _customer_stats

COMPANY = "11111111-1111-1111-1111-111111111111"
USER = {"tenant_id": COMPANY, "sub": "tester"}


@pytest.fixture
def db(tenant_db, monkeypatch):
    monkeypatch.delenv("GDX_ENV", raising=False)
    tenant_db.add(AppSettings(timezone="America/Chicago"))
    ensure_gl_seed(tenant_db, COMPANY)
    tenant_db.commit()
    return tenant_db


def _sent_invoice(db, *, customer_id, job_id, total="100.00"):
    inv = Invoice(
        id=uuid4(),
        customer_id=customer_id,
        job_id=job_id,
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


def test_a_real_credit_memo_nets_lifetime_value_to_80(db):
    cust = Customer(name="Net Billed", email="net@example.com", phone="555-0100", company_id=COMPANY)
    db.add(cust)
    db.commit()
    db.refresh(cust)
    job = Job(
        customer_id=cust.id, title="Spring", description="Broken spring",
        lifecycle_stage="estimate", dispatch_status="unassigned", billing_status="unbilled",
        company_id=COMPANY,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    inv = _sent_invoice(db, customer_id=cust.id, job_id=job.id)
    issue_credit_memo(str(inv.id), CreditMemoIn(amount=20.0, reason="warranty"), db=db, _=USER)
    db.refresh(inv)
    assert inv.total == Decimal("100.00"), "the design stays: a credit never rewrites total"

    row = next(r for r in _customer_stats(db) if str(r["id"]).replace("-", "") == cust.id.hex)
    assert float(row["lifetime_value"]) == 80.0
