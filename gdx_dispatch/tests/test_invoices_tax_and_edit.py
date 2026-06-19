"""Invoice tax-rate mode + line edit/delete.

Doug 2026-05-06 / S110: invoices stored a flat tax_amount with no rate,
so editing a line never recomputed tax. Plus there was no way to edit or
remove a line once added. This file exercises:

- tax_rate-mode: changing a line's qty/price recomputes tax.
- non-taxable lines: labor flagged taxable=False is excluded from the
  taxable subtotal.
- line PATCH and DELETE endpoints exist and recompute totals.
- legacy flat-tax_amount mode (tax_rate=NULL) still honors the stored
  tax_amount unchanged.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models.tenant_models  # noqa: F401
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Job
from gdx_dispatch.routers.invoices import (
    InvoiceCreateIn,
    InvoiceLineCreateIn,
    InvoiceLinePatchIn,
    InvoicePatchIn,
    add_invoice_line,
    create_invoice,
    delete_invoice_line,
    patch_invoice,
    patch_invoice_line,
)


def _user():
    return {"sub": "u-1", "user_id": "u-1", "role": "admin", "tenant_id": "tenant-test"}


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sess = Session()
    yield sess
    sess.close()
    engine.dispose()


def _seed_job(db) -> Job:
    job = Job(
        customer_id=uuid4(),
        title="Install",
        description="",
        lifecycle_stage="estimate",
        dispatch_status="unassigned",
        billing_status="unbilled",
        company_id="tenant-test",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _create_with_lines(db, *, tax_rate, lines):
    job = _seed_job(db)
    payload = InvoiceCreateIn(
        job_id=job.id,
        customer_id=job.customer_id,
        tax_rate=tax_rate,
        line_items=[InvoiceLineCreateIn(**ln) for ln in lines],
        invoice_date=date.today(),
    )
    return create_invoice(payload=payload, _=_user(), db=db)


def test_rate_mode_taxes_only_taxable_lines(db):
    inv = _create_with_lines(
        db,
        tax_rate=0.0738,
        lines=[
            {"description": "Door 10x8", "quantity": 1, "unit_price": 1000.0, "taxable": True},
            {"description": "Labor — install", "quantity": 4, "unit_price": 100.0, "taxable": False},
        ],
    )
    # Subtotal = 1400, taxable subtotal = 1000, tax = 1000 * 0.0738 = 73.80
    assert inv["subtotal"] == 1400.00
    assert inv["taxable_subtotal"] == 1000.00
    assert round(inv["tax_amount"], 2) == 73.80
    assert round(inv["total"], 2) == 1473.80
    assert inv["tax_rate"] == 0.0738


def test_rate_mode_recomputes_after_line_edit(db):
    inv = _create_with_lines(
        db,
        tax_rate=0.0738,
        lines=[
            {"description": "Door 10x8", "quantity": 1, "unit_price": 1000.0, "taxable": True},
        ],
    )
    line_id = UUID([
        ln for ln in (
            patch_invoice_line.__wrapped__.__name__,  # smoke; real id below
        )
    ][0]) if False else None  # placeholder; replaced below
    # Pull the line id from DB.
    line_row = db.execute(
        InvoiceLine.__table__.select().where(InvoiceLine.invoice_id == UUID(inv["id"]))
    ).mappings().first()
    line_id = line_row["id"]

    updated = patch_invoice_line(
        invoice_id=UUID(inv["id"]),
        line_id=line_id,
        payload=InvoiceLinePatchIn(quantity=2),
        user=_user(),
        db=db,
    )
    assert updated["quantity"] == 2
    assert round(updated["line_total"], 2) == 2000.00

    # Re-read invoice via the GET path equivalent (read from DB).
    db.expire_all()
    inv_row = db.get(Invoice, UUID(inv["id"]))
    assert round(float(inv_row.subtotal), 2) == 2000.00
    # Tax should follow: 2000 * 0.0738 = 147.60
    assert round(float(inv_row.tax_amount), 2) == 147.60
    assert round(float(inv_row.total), 2) == 2147.60


def test_line_delete_removes_from_subtotal_and_tax(db):
    inv = _create_with_lines(
        db,
        tax_rate=0.0738,
        lines=[
            {"description": "Door", "quantity": 1, "unit_price": 1000.0, "taxable": True},
            {"description": "Spring", "quantity": 1, "unit_price": 200.0, "taxable": True},
        ],
    )
    rows = db.execute(
        InvoiceLine.__table__.select().where(InvoiceLine.invoice_id == UUID(inv["id"]))
    ).mappings().all()
    spring = next(r for r in rows if r["description"] == "Spring")

    delete_invoice_line(
        invoice_id=UUID(inv["id"]),
        line_id=spring["id"],
        user=_user(),
        db=db,
    )
    db.expire_all()
    inv_row = db.get(Invoice, UUID(inv["id"]))
    assert round(float(inv_row.subtotal), 2) == 1000.00
    assert round(float(inv_row.tax_amount), 2) == 73.80


def test_legacy_flat_tax_amount_path_unchanged(db):
    job = _seed_job(db)
    inv = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            tax_amount=42.50,
            invoice_date=date.today(),
            line_items=[InvoiceLineCreateIn(description="X", quantity=1, unit_price=500.0)],
        ),
        _=_user(), db=db,
    )
    # tax_rate stays None → flat tax_amount honored as-is.
    assert inv["tax_rate"] is None
    assert round(inv["tax_amount"], 2) == 42.50
    assert round(inv["total"], 2) == 542.50

    # Patch tax_amount → still flat-mode → no rate applied.
    updated = patch_invoice(
        invoice_id=UUID(inv["id"]),
        payload=InvoicePatchIn(tax_amount=99.99),
        _=_user(), db=db,
    )
    assert updated["tax_rate"] is None
    assert round(updated["tax_amount"], 2) == 99.99
