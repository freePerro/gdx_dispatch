"""Mobile invoice creation mirrors the desktop double-billing guard (M38).

POST /api/mobile/jobs/{job_id}/invoice had no guard at all: a tech tapping
Generate twice on a slow connection minted two drafts each carrying the same
labor/estimate lines, and only the office review stood between that and the
customer. The desktop create path has 409'd on a billing-real invoice since
2026-07-23; this pins the mobile mirror — same predicate, deliberately NO
force-override (billing a job twice on purpose is an office decision).

Predicate parity (core/billing_predicates.job_billed_exists, lockstep):
void invoices, $0 drafts, and deposit invoices do NOT count as billed.
"""
from __future__ import annotations

import json as _json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase, ensure_audit_table
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job, Technician

TENANT = "tenant-m38"
USER = "tech-user-m38"


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


def _request() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    req.state.tenant_id = TENANT
    return req


def _job_owned_by_tech(db):
    cust = Customer(id=uuid.uuid4(), name="M38 Customer", company_id=TENANT)
    db.add(cust)
    tech = Technician(id=uuid.uuid4().hex, name="M38 Tech", user_id=USER, company_id=TENANT)
    db.add(tech)
    job = Job(id=uuid.uuid4(), title="Opener install", customer_id=cust.id,
              assigned_to=tech.id, company_id=TENANT)
    db.add(job)
    db.commit()
    return job


def _create(db, job):
    from gdx_dispatch.routers.mobile_invoicing import (
        CreateInvoiceIn,
        mobile_create_invoice,
    )

    resp = mobile_create_invoice(
        job_id=job.id.hex,  # SQLite stores Uuid as 32-hex text
        payload=CreateInvoiceIn(send_email=False),
        request=_request(),
        current_user={"user_id": USER, "sub": USER},
        db=db,
    )
    return resp.status_code, _json.loads(resp.body)


def _seed_invoice(db, job, *, total, status, billing_type="standard"):
    inv = Invoice(
        id=uuid.uuid4(), job_id=job.id, customer_id=job.customer_id,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}", billing_type=billing_type,
        sequence_number=1, subtotal=Decimal(str(total)), tax_amount=Decimal("0"),
        total=Decimal(str(total)), balance_due=Decimal(str(total)), status=status,
        public_token=uuid.uuid4().hex, company_id=TENANT,
    )
    db.add(inv)
    db.commit()
    return inv


def test_a_billed_job_refuses_a_second_mobile_invoice(db):
    """THE FIX: the double-tap's second request 409s naming the first
    invoice instead of minting a sibling draft."""
    job = _job_owned_by_tech(db)
    first = _seed_invoice(db, job, total="600.00", status="draft")
    code, body = _create(db, job)
    assert code == 409, body
    assert body["detail"]["code"] == "already_billed"
    assert first.invoice_number in body["detail"]["message"]
    live = db.execute(select(Invoice).where(Invoice.job_id == job.id)).scalars().all()
    assert len(live) == 1, "the refused create must not have written an invoice"


def test_a_voided_invoice_does_not_block(db):
    job = _job_owned_by_tech(db)
    _seed_invoice(db, job, total="600.00", status="void")
    code, _ = _create(db, job)
    assert code == 201


def test_a_deposit_invoice_does_not_block(db):
    """Money BEFORE the work is not billing the work — the deposit-taking
    job keeps its Generate button (predicate parity with the desktop)."""
    job = _job_owned_by_tech(db)
    _seed_invoice(db, job, total="500.00", status="sent", billing_type="deposit")
    code, _ = _create(db, job)
    assert code == 201


def test_a_zero_dollar_draft_does_not_block(db):
    """$0 drafts are placeholders, not billing (predicate parity) — the
    fabricated empty draft must not hide the Generate path."""
    job = _job_owned_by_tech(db)
    _seed_invoice(db, job, total="0.00", status="draft")
    code, _ = _create(db, job)
    assert code == 201


def test_a_sent_zero_dollar_invoice_DOES_block(db):
    """A $0 invoice deliberately SENT is warranty work — it counts as
    billed, same as the desktop predicate."""
    job = _job_owned_by_tech(db)
    _seed_invoice(db, job, total="0.00", status="sent")
    code, body = _create(db, job)
    assert code == 409
    assert body["detail"]["code"] == "already_billed"


def test_a_strangers_job_still_404s_before_the_guard(db):
    """Ownership stays the first gate — the guard must not leak invoice
    numbers for jobs the caller doesn't own."""
    job = _job_owned_by_tech(db)
    _seed_invoice(db, job, total="600.00", status="draft")
    from gdx_dispatch.routers.mobile_invoicing import (
        CreateInvoiceIn,
        mobile_create_invoice,
    )

    resp = mobile_create_invoice(
        job_id=job.id.hex, payload=CreateInvoiceIn(send_email=False),
        request=_request(), current_user={"user_id": "someone-else", "sub": "someone-else"},
        db=db,
    )
    assert resp.status_code == 404
