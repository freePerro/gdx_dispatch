"""GDXA-641 — smart-estimate counts a job as done by lifecycle_stage.

Prod ``jobs.status`` holds both 'Complete' (dispatch) and 'Completed'
(closeout), and imported jobs have no status at all. The price suggestion
keyed on ``status IN ('Completed','completed')`` and so dropped every
'Complete' and NULL-status job from its similar-job average. Here three
completed jobs carry the three spellings in prod; all three count. A job whose
status says 'Completed' but whose lifecycle_stage is not completed does not.
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from gdx_dispatch.core.ai_router import smart_estimate
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job

COMPANY = "11111111-1111-1111-1111-111111111111"


class _Request:
    def __init__(self, body: dict):
        self.state = SimpleNamespace(tenant={"id": COMPANY})
        self._body = body

    async def json(self) -> dict:
        return self._body


def _job_with_invoice(db, customer, *, stage: str, status: str | None, total: str) -> None:
    job = Job(
        id=uuid4(), customer_id=customer.id, title="Spring swap", job_type="repair",
        lifecycle_stage=stage, status=status, company_id=COMPANY,
    )
    db.add(job)
    db.flush()
    db.add(Invoice(
        id=uuid4(), customer_id=customer.id, job_id=job.id,
        invoice_number=f"INV-{uuid4().hex[:8].upper()}", status="sent",
        subtotal=Decimal(total), tax_amount=Decimal("0.00"), total=Decimal(total),
        balance_due=Decimal(total), invoice_date=date(2026, 9, 14),
        public_token=secrets.token_urlsafe(48)[:64], company_id=COMPANY,
    ))


@pytest.fixture
def jobs(tenant_db):
    db = tenant_db
    customer = Customer(id=uuid4(), name="Similar Jobs", company_id=COMPANY)
    db.add(customer)
    db.flush()
    _job_with_invoice(db, customer, stage="completed", status="Complete", total="100.00")
    _job_with_invoice(db, customer, stage="completed", status="Completed", total="200.00")
    _job_with_invoice(db, customer, stage="completed", status=None, total="300.00")
    _job_with_invoice(db, customer, stage="in_progress", status="Completed", total="9000.00")
    db.commit()
    return db


def test_smart_estimate_counts_every_completed_job_whatever_its_status(jobs):
    out = asyncio.run(smart_estimate(_Request({"job_type": "repair"}), db=jobs, user={}))
    assert out["similar_jobs_count"] == 3
    assert out["suggested_price"] == pytest.approx(200.0)
