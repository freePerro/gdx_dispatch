"""Deposit invoices (2026-07-23) — downpayments at estimate acceptance.

Pins the three money rules from modules/deposits/service.py:

1. A deposit invoice never bills the job (billed predicate exclusion).
2. The final invoice nets the PAID deposit with a negative line — no 150%
   double-count of what the customer owes.
3. An UNPAID deposit remainder is superseded via credit memo at final-invoice
   time — accept-then-abandon can't leave the customer owing deposit + total.

Plus the glue: office accept creates job + deposit together, and a deposit
born before the job existed (mobile accept) is adopted at conversion.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.billing_predicates import job_billed_exists
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceAdjustment,
    InvoiceLine,
    Job,
    JobPartNeeded,
    Payment,
)
from gdx_dispatch.modules.deposits import (
    DepositError,
    create_deposit_invoice,
    find_deposit_invoice_for_estimate,
)
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.invoices import InvoiceCreateIn, create_invoice

USER = {"user_id": "user-1", "tenant_id": "tenant-1", "role": "admin", "sub": "user-1"}


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [
        Job.__table__,
        Customer.__table__,
        Invoice.__table__,
        InvoiceLine.__table__,
        Payment.__table__,
        InvoiceAdjustment.__table__,
        JobPartNeeded.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_customer(db) -> Customer:
    cust = Customer(id=uuid4(), name="Depo Sit", company_id="tenant-1")
    db.add(cust)
    db.commit()
    return cust


def _seed_estimate(db, customer, *, total: float = 1000.0, job=None, status="accepted") -> Estimate:
    est = Estimate(
        id=uuid4(),
        job_id=(job.id if job is not None else None),
        customer_id=customer.id,
        estimate_number=f"EST-{uuid4().hex[:8]}",
        label="16x7 door",
        proposal_mode=False,
        total=Decimal(str(total)),
        status=status,
        public_token=uuid4().hex,
        company_id="tenant-1",
    )
    db.add(est)
    db.flush()
    db.add(EstimateLine(
        estimate_id=est.id,
        description="Door + install",
        quantity=1,
        unit_price=Decimal(str(total)),
        line_total=Decimal(str(total)),
        sort_order=1,
        company_id="tenant-1",
    ))
    db.commit()
    db.refresh(est)
    return est


def _seed_job(db, customer) -> Job:
    job = Job(
        id=uuid4(),
        customer_id=customer.id,
        title="Install",
        lifecycle_stage="scheduled",
        dispatch_status="unassigned",
        billing_status="unbilled",
        company_id="tenant-1",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _pay(db, invoice, amount: float) -> None:
    from gdx_dispatch.routers.invoices import _recalculate_invoice

    db.add(Payment(
        company_id="tenant-1",
        invoice_id=invoice.id,
        amount=Decimal(str(amount)),
        method="card",
        payment_date=date.today(),
    ))
    db.flush()
    _recalculate_invoice(invoice, db)
    db.commit()


def _make_deposit(db, est, amount=250.0):
    return create_deposit_invoice(
        db, estimate=est, amount=amount, tenant_id="tenant-1",
        actor="user-1", source="test",
    )


def _make_final(db, est, job, *, force: bool = False):
    payload = InvoiceCreateIn(
        job_id=job.id, estimate_id=est.id, customer_id=est.customer_id, force=force,
    )
    return create_invoice(payload, USER, db)


# ---------------------------------------------------------------------------
# Rule 0 — creation semantics
# ---------------------------------------------------------------------------

def test_create_deposit_invoice_basic_and_idempotent(db):
    cust = _seed_customer(db)
    job = _seed_job(db, cust)
    est = _seed_estimate(db, cust, job=job)

    inv = _make_deposit(db, est, 250.0)
    assert inv.billing_type == "deposit"
    assert inv.status == "sent"
    assert float(inv.total) == 250.0
    assert float(inv.balance_due) == 250.0
    assert inv.estimate_id == est.id
    assert inv.job_id == job.id
    line = db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
    ).scalar_one()
    assert line.taxable is False
    assert line.category == "Deposit"

    # Second call returns the SAME invoice — double-tapped accept buttons.
    again = _make_deposit(db, est, 999.0)
    assert again.id == inv.id
    assert float(again.total) == 250.0


def test_deposit_exceeding_estimate_total_refused(db):
    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, total=500.0)
    with pytest.raises(DepositError):
        _make_deposit(db, est, 600.0)


def test_deposit_requires_customer(db):
    cust = _seed_customer(db)
    est = _seed_estimate(db, cust)
    est.customer_id = None
    db.commit()
    with pytest.raises(DepositError):
        _make_deposit(db, est, 100.0)


# ---------------------------------------------------------------------------
# Rule 1 — a deposit never bills the job
# ---------------------------------------------------------------------------

def test_deposit_does_not_bill_job(db):
    cust = _seed_customer(db)
    job = _seed_job(db, cust)
    est = _seed_estimate(db, cust, job=job)
    _make_deposit(db, est, 250.0)

    billed = db.execute(select(Job.id).where(job_billed_exists())).scalars().all()
    assert billed == []

    _make_final(db, est, job)
    billed = db.execute(select(Job.id).where(job_billed_exists())).scalars().all()
    assert [str(b) for b in billed] == [str(job.id)]


# ---------------------------------------------------------------------------
# Rule 2 — the final invoice nets the PAID deposit
# ---------------------------------------------------------------------------

def test_final_invoice_nets_paid_deposit(db):
    cust = _seed_customer(db)
    job = _seed_job(db, cust)
    est = _seed_estimate(db, cust, total=1000.0, job=job)
    dep = _make_deposit(db, est, 250.0)
    _pay(db, dep, 250.0)
    db.refresh(dep)
    assert dep.status == "paid"

    resp = _make_final(db, est, job)
    assert resp["deposit_netting"]["deposit_paid_applied"] == 250.0
    final = db.get(Invoice, UUID(resp["id"]))
    assert float(final.total) == 750.0
    assert float(final.balance_due) == 750.0
    neg = [
        ln for ln in db.execute(
            select(InvoiceLine).where(InvoiceLine.invoice_id == final.id)
        ).scalars().all()
        if float(ln.line_total or 0) < 0
    ]
    assert len(neg) == 1
    assert float(neg[0].line_total) == -250.0
    assert neg[0].category == "Deposit"
    assert dep.invoice_number in neg[0].description
    # The paid deposit invoice is untouched — no credit memo needed.
    assert db.execute(
        select(InvoiceAdjustment).where(InvoiceAdjustment.invoice_id == dep.id)
    ).scalars().all() == []


# ---------------------------------------------------------------------------
# Rule 3 — an UNPAID deposit remainder is superseded (accept-then-abandon)
# ---------------------------------------------------------------------------

def test_unpaid_deposit_voided_on_final(db):
    """Accept-then-abandon: a wholly-unpaid deposit is VOIDED at final-create
    (implementation-audit catch: the credit-memo settle showed a never-paid
    invoice as 'paid', and record_payment would take a late check onto it)."""
    from gdx_dispatch.routers.invoices import PaymentCreateIn, record_payment
    from fastapi import HTTPException

    cust = _seed_customer(db)
    job = _seed_job(db, cust)
    est = _seed_estimate(db, cust, total=1000.0, job=job)
    dep = _make_deposit(db, est, 250.0)  # customer abandoned payment

    resp = _make_final(db, est, job)
    assert resp["deposit_netting"]["deposit_paid_applied"] == 0.0
    assert resp["deposit_netting"]["voided"] == [dep.invoice_number]
    assert resp["deposit_netting"]["superseded"] == []

    final = db.get(Invoice, UUID(resp["id"]))
    db.refresh(dep)
    # Full total on the final; the abandoned deposit reads honestly as void —
    # never as a fake "paid".
    assert float(final.total) == 1000.0
    assert dep.status == "void"
    assert dep.paid_at is None
    assert float(dep.balance_due) == 0.0
    # The late-arriving check cannot land on the dead deposit.
    with pytest.raises(HTTPException) as exc:
        record_payment(dep.id, PaymentCreateIn(amount=250.0, method="check"), USER, db)
    assert exc.value.status_code == 409
    # Customer's total exposure across both invoices is exactly the job total.
    assert float(final.balance_due) + float(dep.balance_due) == 1000.0


def test_partially_paid_deposit_nets_paid_and_credits_rest(db):
    from gdx_dispatch.routers.invoices import PaymentCreateIn, record_payment
    from fastapi import HTTPException

    cust = _seed_customer(db)
    job = _seed_job(db, cust)
    est = _seed_estimate(db, cust, total=1000.0, job=job)
    dep = _make_deposit(db, est, 250.0)
    _pay(db, dep, 100.0)

    resp = _make_final(db, est, job)
    assert resp["deposit_netting"]["deposit_paid_applied"] == 100.0
    assert resp["deposit_netting"]["superseded"] == [dep.invoice_number]
    assert resp["deposit_netting"]["voided"] == []

    final = db.get(Invoice, UUID(resp["id"]))
    db.refresh(dep)
    assert float(final.total) == 900.0
    assert float(dep.balance_due) == 0.0
    # Exposure: 900 remaining + 100 already paid = 1000 job total.
    assert float(final.balance_due) == 900.0
    # A late payment on the superseded deposit is refused with a pointer to
    # the final invoice (implementation-audit catch: double-charge door).
    with pytest.raises(HTTPException) as exc:
        record_payment(dep.id, PaymentCreateIn(amount=150.0, method="check"), USER, db)
    assert exc.value.status_code == 409
    assert "final invoice" in exc.value.detail


def test_second_final_requires_force_and_deposit_not_applied_twice(db):
    from fastapi import HTTPException

    cust = _seed_customer(db)
    job = _seed_job(db, cust)
    est = _seed_estimate(db, cust, total=1000.0, job=job)
    dep = _make_deposit(db, est, 250.0)
    _pay(db, dep, 250.0)

    first = _make_final(db, est, job)
    assert first["deposit_netting"]["deposit_paid_applied"] == 250.0

    # Without force, a second final on an already-billed job 409s (the
    # explicit double-billing guard on the path the UI actually uses).
    with pytest.raises(HTTPException) as exc:
        _make_final(db, est, job)
    assert exc.value.status_code == 409
    assert "already billed" in exc.value.detail

    # Forced second final: allowed, but it must NOT subtract the same
    # deposit again — the prior netting line blocks re-application.
    second = _make_final(db, est, job, force=True)
    netting = second.get("deposit_netting") or {}
    assert "skipped" in netting
    assert netting.get("deposit_paid_applied") is None
    final2 = db.get(Invoice, UUID(second["id"]))
    assert float(final2.total) == 1000.0


def test_netting_line_cannot_be_deleted_or_edited(db):
    from fastapi import HTTPException
    from gdx_dispatch.routers.invoices import (
        InvoiceLinePatchIn,
        delete_invoice_line,
        patch_invoice_line,
    )

    cust = _seed_customer(db)
    job = _seed_job(db, cust)
    est = _seed_estimate(db, cust, total=1000.0, job=job)
    dep = _make_deposit(db, est, 250.0)
    _pay(db, dep, 250.0)
    resp = _make_final(db, est, job)
    final_id = UUID(resp["id"])
    neg = next(
        ln for ln in db.execute(
            select(InvoiceLine).where(InvoiceLine.invoice_id == final_id)
        ).scalars().all()
        if float(ln.line_total or 0) < 0
    )
    # Deleting the netting line would silently re-bill the collected deposit.
    with pytest.raises(HTTPException) as exc:
        delete_invoice_line(final_id, neg.id, USER, db)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        patch_invoice_line(final_id, neg.id, InvoiceLinePatchIn(unit_price=1.0), USER, db)
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# Glue — office accept creates job + deposit; orphan adoption
# ---------------------------------------------------------------------------

def test_office_accept_creates_job_and_deposit(db):
    from gdx_dispatch.routers.estimates import AcceptEstimateIn, accept_estimate

    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, total=1000.0, status="sent")

    resp = accept_estimate(
        est.id, AcceptEstimateIn(deposit_amount=300.0), USER, db
    )
    assert resp["status"] == "accepted"
    assert resp.get("auto_converted_job_id")
    dep_info = resp.get("deposit")
    assert dep_info and dep_info["amount"] == 300.0

    dep = find_deposit_invoice_for_estimate(db, est.id)
    assert dep is not None
    assert str(dep.job_id) == resp["auto_converted_job_id"]
    assert dep.status == "sent"


def test_office_accept_without_deposit_unchanged(db):
    from gdx_dispatch.routers.estimates import accept_estimate

    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, total=1000.0, status="sent")
    resp = accept_estimate(est.id, None, USER, db)
    assert resp["status"] == "accepted"
    assert "deposit" not in resp
    assert find_deposit_invoice_for_estimate(db, est.id) is None


def test_request_deposit_invoice_after_acceptance(db):
    """Retroactive path (Doug 2026-07-23: 'no way of applying deposits' on
    estimates accepted before the feature): explicit request endpoint,
    accepted-only, idempotent per estimate."""
    from fastapi import HTTPException
    from gdx_dispatch.routers.estimates import DepositInvoiceIn, request_deposit_invoice

    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, total=1000.0, status="sent")
    with pytest.raises(HTTPException) as exc:
        request_deposit_invoice(est.id, DepositInvoiceIn(amount=100.0), USER, db)
    assert exc.value.status_code == 409

    est.status = "accepted"
    db.commit()
    out = request_deposit_invoice(est.id, DepositInvoiceIn(amount=300.0), USER, db)
    assert out["existing"] is False
    assert out["amount"] == 300.0
    again = request_deposit_invoice(est.id, DepositInvoiceIn(amount=999.0), USER, db)
    assert again["existing"] is True
    assert again["invoice_id"] == out["invoice_id"]


def test_request_deposit_invoice_defaults_to_tenant_pct(db):
    """No explicit amount → tenant deposit percent × estimate total. The
    hermetic env can't read the control DB, so get_features returns the
    default EstimatesFeatures (deposit_pct=50) — 50% of $1000 = $500."""
    from gdx_dispatch.routers.estimates import request_deposit_invoice

    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, total=1000.0, status="accepted")
    out = request_deposit_invoice(est.id, None, USER, db)
    assert out["existing"] is False
    assert out["amount"] == 500.0


def test_orphan_deposit_adopted_at_job_conversion(db):
    """Mobile accept creates no job — the deposit is born job-less and must
    be adopted when the estimate converts, or final-invoice netting never
    finds it."""
    from gdx_dispatch.routers.estimates import _create_job_from_estimate

    cust = _seed_customer(db)
    est = _seed_estimate(db, cust, total=1000.0)  # no job
    dep = _make_deposit(db, est, 250.0)
    assert dep.job_id is None

    new_job = _create_job_from_estimate(est, db, "user-1")
    db.refresh(dep)
    assert dep.job_id == new_job.id
