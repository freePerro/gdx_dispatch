"""PR3-billing-capture (2026-07-07) — change orders reach the invoice.

Approved change orders were captured, signed, then orphaned: NO code path
turned a ChangeOrderLine into an InvoiceLine — a primary money leak. Pinned
here:

1. **The stamp gates the copy.** Billing a CO stamps `billed_invoice_id` via
   UPDATE…RETURNING and only RETURNING'd COs get lines copied. Billing the
   same CO on a second invoice → 409 and the second invoice is NOT created
   (copy-then-stamp would have double-billed the customer — the failure mode
   worse than the disease).
2. **Tax parity (Doug 2026-07-07: "handled like an invoice").** The total
   shown on the CO detail equals the invoice total for the same lines, both
   using the same rate resolver.
3. Delete-invoice releases the CO back to the unbilled checklist.
4. One-click create_invoice_from_job pulls approved unbilled COs (with tax on
   top of the estimate-derived totals).
5. `GET /api/change-orders?unbilled=true` filter.
6. The S122 parts path retrofit: an already-billed part now 409s instead of
   silently double-billing through the payload lines.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import (
    ChangeOrderLine,
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    JobPartNeeded,
    Payment,
)
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption
from gdx_dispatch.routers.change_orders import (
    ChangeOrder,
    ChangeOrderIn,
    _serialize,
    create_change_order,
    list_change_orders,
)
from gdx_dispatch.routers.invoices import (
    InvoiceCreateIn,
    create_invoice,
    delete_invoice,
)

TAX_RATE = 0.08


@pytest.fixture
def tenant_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    for tbl in [
        Job.__table__,
        Customer.__table__,
        Estimate.__table__,
        EstimateLine.__table__,
        Invoice.__table__,
        InvoiceLine.__table__,
        Payment.__table__,
        JobPartNeeded.__table__,
        ChangeOrder.__table__,
        ChangeOrderLine.__table__,
        TaxConfig.__table__,
        TaxExemption.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)

    db = Session()
    db.add(TaxConfig(default_rate=Decimal(str(TAX_RATE))))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _current_user() -> dict[str, str]:
    return {"user_id": "user-1", "tenant_id": "tenant-1", "role": "admin"}


def _seed_job(db) -> Job:
    job = Job(
        customer_id=uuid4(),
        title="Door install",
        description="t",
        lifecycle_stage="completed",
        dispatch_status="done",
        billing_status="unbilled",
        company_id="tenant-1",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _seed_co(db, job, *, approved: bool = True) -> ChangeOrder:
    """Create an approved CO with one taxable line ($300) and one tax-exempt
    labor line ($200) through the real create handler."""
    payload = ChangeOrderIn(
        job_id=str(job.id),
        customer_id=str(job.customer_id),
        title="Extra opener + labor",
        status="approved" if approved else "draft",
        line_items=[
            {"description": "Opener unit", "quantity": 1, "unit_price": 300.0},
            {"description": "Labor", "quantity": 2, "unit_price": 100.0, "taxable": False},
        ],
    )
    out = create_change_order(payload=payload, user=_current_user(), db=db)
    return db.get(ChangeOrder, UUID(out["id"]))


def test_stamp_gates_copy_and_second_invoice_409s(tenant_db_session):
    db = tenant_db_session
    job = _seed_job(db)
    co = _seed_co(db, job)

    first = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            from_change_order_ids=[co.id],
        ),
        _=_current_user(),
        db=db,
    )
    db.refresh(co)
    assert str(co.billed_invoice_id) == first["id"]
    lines = db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == UUID(first["id"]))
    ).scalars().all()
    assert len(lines) == 2
    assert all(ln.description.startswith(co.co_number) for ln in lines)
    assert {bool(ln.taxable) for ln in lines} == {True, False}

    invoice_count_before = db.scalar(select(func.count(Invoice.id)))
    with pytest.raises(HTTPException) as exc_info:
        create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                from_change_order_ids=[co.id],
            ),
            _=_current_user(),
            db=db,
        )
    assert exc_info.value.status_code == 409
    # The whole second create rolled back: no new invoice, no copied lines,
    # CO still owned by the FIRST invoice.
    assert db.scalar(select(func.count(Invoice.id))) == invoice_count_before
    db.refresh(co)
    assert str(co.billed_invoice_id) == first["id"]
    total_lines = db.scalar(select(func.count(InvoiceLine.id)))
    assert total_lines == 2


def test_unapproved_co_cannot_be_billed(tenant_db_session):
    db = tenant_db_session
    job = _seed_job(db)
    co = _seed_co(db, job, approved=False)

    with pytest.raises(HTTPException) as exc_info:
        create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                from_change_order_ids=[co.id],
            ),
            _=_current_user(),
            db=db,
        )
    assert exc_info.value.status_code == 409


def test_signed_co_total_equals_invoice_total(tenant_db_session):
    """Doug 2026-07-07: the customer sees tax on the CO and signs the same
    total the invoice bills. Both sides use resolve_rate → TaxConfig 8%.
    Taxable $300 → $24 tax; exempt labor $200 → subtotal 500, total 524."""
    db = tenant_db_session
    job = _seed_job(db)
    co = _seed_co(db, job)

    co_lines = db.execute(
        select(ChangeOrderLine).where(ChangeOrderLine.co_id == co.id)
    ).scalars().all()
    co_view = _serialize(co, lines=co_lines, db=db)
    assert co_view["subtotal"] == 500.0
    assert co_view["tax_amount"] == 24.0
    assert co_view["total"] == 524.0

    created = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            from_change_order_ids=[co.id],
        ),
        _=_current_user(),
        db=db,
    )
    inv = db.get(Invoice, UUID(created["id"]))
    assert float(inv.subtotal) == co_view["subtotal"]
    assert float(inv.tax_amount) == co_view["tax_amount"]
    assert float(inv.total) == co_view["total"]


def test_delete_invoice_releases_change_order(tenant_db_session):
    db = tenant_db_session
    job = _seed_job(db)
    co = _seed_co(db, job)
    created = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            from_change_order_ids=[co.id],
        ),
        _=_current_user(),
        db=db,
    )

    delete_invoice(UUID(created["id"]), _=_current_user(), db=db)

    db.refresh(co)
    assert co.billed_invoice_id is None
    unbilled = list_change_orders(
        _=_current_user(), db=db, job_id=str(job.id), unbilled=True
    )
    assert [c["id"] for c in unbilled] == [str(co.id)]


def test_unbilled_filter_excludes_billed_and_unapproved(tenant_db_session):
    db = tenant_db_session
    job = _seed_job(db)
    billed_co = _seed_co(db, job)
    draft_co = _seed_co(db, job, approved=False)
    open_co = _seed_co(db, job)
    create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            from_change_order_ids=[billed_co.id],
        ),
        _=_current_user(),
        db=db,
    )

    unbilled = list_change_orders(
        _=_current_user(), db=db, job_id=str(job.id), unbilled=True
    )
    ids = {c["id"] for c in unbilled}
    assert str(open_co.id) in ids
    assert str(billed_co.id) not in ids
    assert str(draft_co.id) not in ids


def test_one_click_invoice_pulls_approved_cos_with_tax(tenant_db_session, monkeypatch):
    db = tenant_db_session
    job = _seed_job(db)
    co = _seed_co(db, job)

    from starlette.requests import Request

    from gdx_dispatch.routers.jobs import create_invoice_from_job
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.tenant = {"id": "tenant-1"}
    out = create_invoice_from_job(
        job_id=str(job.id), request=request,
        current_user={"sub": "user-1", "tenant_id": "tenant-1"}, db=db,
    )
    # No estimate on the job → $0 fallback line + CO lines w/ tax on top:
    # subtotal 500, tax 24 (taxable 300 × 8%), total 524.
    assert out["total"] == 524.0
    db.refresh(co)
    assert str(co.billed_invoice_id) == out["invoice_id"]
    lines = db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == UUID(out["invoice_id"]))
    ).scalars().all()
    co_lines = [ln for ln in lines if ln.description.startswith(co.co_number)]
    assert len(co_lines) == 2


def test_already_billed_part_409s_instead_of_silent_double_bill(tenant_db_session):
    """S122 retrofit: the old UPDATE…WHERE IS NULL silently skipped a part
    another invoice owned while the payload lines still carried the amount —
    a quiet double-bill. Now the whole request 409s."""
    db = tenant_db_session
    job = _seed_job(db)
    part = JobPartNeeded(
        id=str(uuid4()),
        company_id="tenant-1",
        job_id=str(job.id),
        part_name="Torsion spring",
        quantity=1,
    )
    db.add(part)
    db.commit()

    first = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            line_items=[{"description": "Torsion spring", "quantity": 1, "unit_price": 80.0}],
            from_part_ids=[UUID(part.id)],
        ),
        _=_current_user(),
        db=db,
    )
    db.refresh(part)
    assert str(part.billed_invoice_id) == first["id"]

    count_before = db.scalar(select(func.count(Invoice.id)))
    with pytest.raises(HTTPException) as exc_info:
        create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                line_items=[{"description": "Torsion spring", "quantity": 1, "unit_price": 80.0}],
                from_part_ids=[UUID(part.id)],
            ),
            _=_current_user(),
            db=db,
        )
    assert exc_info.value.status_code == 409
    assert db.scalar(select(func.count(Invoice.id))) == count_before


def test_co_requires_job_id_at_contract():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        InvoiceCreateIn(customer_id=uuid4(), from_change_order_ids=[uuid4()])


# ---------------------------------------------------------------------------
# Audit round 2 — amount-only COs (the reproduced money-loser) + freeze guards
# ---------------------------------------------------------------------------


def _seed_amount_only_co(db, job, *, amount: float = 500.0, approved: bool = True) -> ChangeOrder:
    """Mobile-dialog shape: flat amount, NO line items (also every legacy
    pre-D-S122 CO)."""
    payload = ChangeOrderIn(
        job_id=str(job.id),
        customer_id=str(job.customer_id),
        title="Field upsell",
        status="approved" if approved else "draft",
        amount=amount,
    )
    out = create_change_order(payload=payload, user=_current_user(), db=db)
    return db.get(ChangeOrder, UUID(out["id"]))


def test_amount_only_co_bills_its_signed_amount(tenant_db_session):
    """Audit round 2 reproduced this leak live: the stamp claimed an
    amount-only CO while the copy produced ZERO lines — $500 signed, $0
    invoiced, gone from the checklist forever. Now the signed amount is
    synthesized as one taxable line."""
    db = tenant_db_session
    job = _seed_job(db)
    co = _seed_amount_only_co(db, job, amount=500.0)

    created = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            from_change_order_ids=[co.id],
        ),
        _=_current_user(),
        db=db,
    )
    lines = db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == UUID(created["id"]))
    ).scalars().all()
    assert len(lines) == 1
    assert float(lines[0].line_total) == 500.0
    assert lines[0].description.startswith(co.co_number)
    inv = db.get(Invoice, UUID(created["id"]))
    # 8% on the synthesized (taxable) amount
    assert float(inv.total) == 540.0
    db.refresh(co)
    assert str(co.billed_invoice_id) == created["id"]


def test_zero_value_lineless_co_409s_not_written_off(tenant_db_session):
    db = tenant_db_session
    job = _seed_job(db)
    co = _seed_amount_only_co(db, job, amount=0.0)

    with pytest.raises(HTTPException) as exc_info:
        create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                from_change_order_ids=[co.id],
            ),
            _=_current_user(),
            db=db,
        )
    assert exc_info.value.status_code == 409
    db.refresh(co)
    assert co.billed_invoice_id is None, "zero-value CO must NOT be claimed"


def test_one_click_bills_amount_only_and_skips_zero_value(tenant_db_session):
    from starlette.requests import Request

    from gdx_dispatch.routers.jobs import create_invoice_from_job
    db = tenant_db_session
    job = _seed_job(db)
    amount_co = _seed_amount_only_co(db, job, amount=250.0)
    zero_co = _seed_amount_only_co(db, job, amount=0.0)

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.tenant = {"id": "tenant-1"}
    out = create_invoice_from_job(
        job_id=str(job.id), request=request,
        current_user={"sub": "user-1", "tenant_id": "tenant-1"}, db=db,
    )
    # $0 fallback line + synthesized $250 CO line, 8% tax on 250 → 270.
    assert out["total"] == 270.0
    db.refresh(amount_co)
    db.refresh(zero_co)
    assert str(amount_co.billed_invoice_id) == out["invoice_id"]
    assert zero_co.billed_invoice_id is None, (
        "a CO with nothing to bill must stay on the checklist, not be "
        "stamped billed at $0"
    )


def test_customer_mismatch_co_409s(tenant_db_session):
    """Audit round 2 blind spot: a CO signed by a DIFFERENT customer must not
    bill onto this invoice (exemption/tax parity would silently diverge)."""
    db = tenant_db_session
    job = _seed_job(db)
    co = _seed_co(db, job)
    co.customer_id = uuid4()  # someone else signed it
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        create_invoice(
            payload=InvoiceCreateIn(
                job_id=job.id,
                customer_id=job.customer_id,
                from_change_order_ids=[co.id],
            ),
            _=_current_user(),
            db=db,
        )
    assert exc_info.value.status_code == 409
    db.refresh(co)
    assert co.billed_invoice_id is None


def test_billed_co_is_frozen_against_patch_and_delete(tenant_db_session):
    from gdx_dispatch.routers.change_orders import delete_change_order, update_change_order
    db = tenant_db_session
    job = _seed_job(db)
    co = _seed_co(db, job)
    create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            from_change_order_ids=[co.id],
        ),
        _=_current_user(),
        db=db,
    )

    with pytest.raises(HTTPException) as patch_exc:
        update_change_order(
            co.id,
            payload=ChangeOrderIn(title="Sneaky edit", amount=9999.0),
            _=_current_user(),
            db=db,
        )
    assert patch_exc.value.status_code == 409

    with pytest.raises(HTTPException) as del_exc:
        delete_change_order(co.id, _=_current_user(), db=db)
    assert del_exc.value.status_code == 409
    db.refresh(co)
    assert co.deleted_at is None
    assert co.billed_invoice_id is not None
