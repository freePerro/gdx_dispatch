"""GDXA-739: an invoice line's total is quantity × the unit_price it stores.

The class: *a line total derived from a different price than the one stored*.
The price is rounded half-up to cents when it is stored, so a total computed
from the raw request price disagrees with the row: 3 × 0.005 stored 0.01 per
unit and a 0.02 total, where 3 × 0.01 is 0.03. The edit path already
recomputed from the stored price, so merely editing such a line changed its
total. Every write path in routers/invoices.py is driven here with a
sub-cent price whose two derivations differ.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Job, JobPartNeeded, Payment
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.invoices import (
    InvoiceCreateIn,
    InvoiceLineCreateIn,
    InvoiceLinePatchIn,
    add_invoice_line,
    create_invoice,
    get_invoice,
    patch_invoice_line,
)

USER = {"user_id": "00000000-0000-0000-0000-000000000739", "tenant_id": "tenant-1", "role": "admin"}

# (raw price, quantity, stored price, line total = quantity × stored price)
CASES = [
    (0.005, 3, Decimal("0.01"), Decimal("0.03")),
    (1.005, 3, Decimal("1.01"), Decimal("3.03")),
]


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for tbl in (Job, Estimate, EstimateLine, Invoice, InvoiceLine, Payment, JobPartNeeded):
        tbl.__table__.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()
    engine.dispose()


def _job(db) -> Job:
    job = Job(customer_id=uuid4(), title="Door repair", description="t", lifecycle_stage="completed",
              dispatch_status="done", billing_status="unbilled", company_id="tenant-1")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _goods(db, invoice_id: UUID) -> list[InvoiceLine]:
    return list(db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id, InvoiceLine.category.is_(None))
    ).scalars())


@pytest.mark.parametrize(("raw", "qty", "stored", "total"), CASES)
def test_create_stores_a_total_that_is_quantity_times_the_stored_price(db, raw, qty, stored, total):
    job = _job(db)
    inv = create_invoice(payload=InvoiceCreateIn(
        job_id=job.id, customer_id=job.customer_id,
        line_items=[InvoiceLineCreateIn(description="Part", quantity=qty, unit_price=raw)],
    ), _=USER, db=db)
    inv_id = UUID(inv["id"])
    [line] = _goods(db, inv_id)
    assert Decimal(str(line.unit_price)) == stored
    assert Decimal(str(line.line_total)) == total == stored * qty
    assert Decimal(str(get_invoice(inv_id, _=USER, db=db)["subtotal"])) == total


@pytest.mark.parametrize(("raw", "qty", "stored", "total"), CASES)
def test_add_line_stores_a_total_that_is_quantity_times_the_stored_price(db, raw, qty, stored, total):
    job = _job(db)
    inv_id = UUID(create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id),
                                 _=USER, db=db)["id"])
    add_invoice_line(invoice_id=inv_id,
                     payload=InvoiceLineCreateIn(description="Part", quantity=qty, unit_price=raw),
                     current_user=USER, db=db)
    [line] = _goods(db, inv_id)
    assert Decimal(str(line.unit_price)) == stored
    assert Decimal(str(line.line_total)) == total == stored * qty
    assert Decimal(str(get_invoice(inv_id, _=USER, db=db)["subtotal"])) == total


def test_a_quantity_edit_recomputes_to_the_total_the_add_path_stored(db):
    """The edit path recomputes from the stored price; re-saving the same
    quantity must land on the total the add path wrote, not move it."""
    job = _job(db)
    inv_id = UUID(create_invoice(payload=InvoiceCreateIn(job_id=job.id, customer_id=job.customer_id),
                                 _=USER, db=db)["id"])
    add_invoice_line(invoice_id=inv_id,
                     payload=InvoiceLineCreateIn(description="Part", quantity=3, unit_price=0.005),
                     current_user=USER, db=db)
    [line] = _goods(db, inv_id)
    before = Decimal(str(line.line_total))
    out = patch_invoice_line(inv_id, line.id, InvoiceLinePatchIn(quantity=3), USER, db)
    assert Decimal(str(out["line_total"])) == before == Decimal("0.03")


def test_discount_cap_measures_the_goods_as_stored(db):
    """The over-discount cap sums the goods the invoice will actually carry.
    Measured from the raw price, 3 × 0.005 is 0.015 and a 0.03 discount on
    0.03 of stored goods was refused as larger than the goods."""
    job = _job(db)
    inv = create_invoice(payload=InvoiceCreateIn(
        job_id=job.id, customer_id=job.customer_id, discount=0.03,
        line_items=[InvoiceLineCreateIn(description="Part", quantity=3, unit_price=0.005)],
    ), _=USER, db=db)
    assert Decimal(str(get_invoice(UUID(inv["id"]), _=USER, db=db)["subtotal"])) == Decimal("0.00")

    job2 = _job(db)
    with pytest.raises(HTTPException) as exc:
        create_invoice(payload=InvoiceCreateIn(
            job_id=job2.id, customer_id=job2.customer_id, discount=0.04,
            line_items=[InvoiceLineCreateIn(description="Part", quantity=3, unit_price=0.005)],
        ), _=USER, db=db)
    assert exc.value.status_code == 422
