"""Vendor invoice line confirmation — effects-layer tests.

Builds an invoice + lines + a job + an inventory item in an isolated tenant DB
and asserts each disposition produces exactly the right downstream records,
plus idempotency and the guard rules. No PDF needed.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from gdx_dispatch.models.tenant_models import (
    Expense,
    InventoryItem,
    Job,
    JobPartNeeded,
    StockAdjustment,
)
from gdx_dispatch.modules.vendor_invoices.confirm import ConfirmError, confirm_line
from gdx_dispatch.modules.vendor_invoices.models import (
    KIND_FREIGHT,
    KIND_ITEM,
    LINE_CONFIRMED,
    VendorInvoice,
    VendorInvoiceLine,
)

TID = "tenant-test"


def _invoice_with_lines(db, *, matched_job_id=None):
    inv = VendorInvoice(
        vendor_name_raw="Midwest Wholesale Doors",
        invoice_number="90000001",
        invoice_date=date(2026, 6, 30),
        subtotal=Decimal("250.00"),
        tax=Decimal("0.00"),
        shipping=Decimal("25.00"),
        total=Decimal("275.00"),
        matched_job_id=matched_job_id,
    )
    inv.lines = [
        VendorInvoiceLine(
            line_no=0, kind=KIND_ITEM, item_label="Garage Door Material",
            description="MODEL-A 9x7 White Panel", quantity=Decimal("2"),
            unit_cost=Decimal("100.0000"), line_total=Decimal("200.00"),
        ),
        VendorInvoiceLine(
            line_no=1, kind=KIND_ITEM, item_label="Garage Door Material",
            description="MODEL-B 16x7 White Panel", quantity=Decimal("1"),
            unit_cost=Decimal("50.0000"), line_total=Decimal("50.00"),
        ),
        VendorInvoiceLine(
            line_no=None, kind=KIND_FREIGHT, item_label="Shipping & Handling",
            description="Shipping & Handling", quantity=Decimal("1"),
            unit_cost=Decimal("25.00"), line_total=Decimal("25.00"),
        ),
    ]
    db.add(inv)
    db.flush()
    return inv


def _job(db):
    job = Job(title="Example garage door job", company_id=TID)
    db.add(job)
    db.flush()
    return job


# --------------------------------------------------------------------------- #
# job disposition
# --------------------------------------------------------------------------- #
def test_job_line_creates_expense_and_billing_row(tenant_db):
    job = _job(tenant_db)
    inv = _invoice_with_lines(tenant_db, matched_job_id=job.id)
    item_line = inv.lines[0]

    confirm_line(tenant_db, inv, item_line, disposition="job",
                 company_id=TID, actor_id="u1")

    exp = tenant_db.query(Expense).one()
    assert exp.source == "vendor_invoice"
    assert exp.category == "materials"
    assert exp.job_id == job.id
    assert exp.amount == Decimal("200.00")
    assert exp.vendor == "Midwest Wholesale Doors"

    jpn = tenant_db.query(JobPartNeeded).one()
    assert jpn.source == "vendor_invoice"
    assert jpn.status == "received"
    assert jpn.job_id == str(job.id)
    assert jpn.quantity == 2
    assert jpn.unit_price is None  # office prices it

    assert item_line.status == LINE_CONFIRMED
    assert item_line.expense_id == exp.id
    assert item_line.job_part_needed_id == jpn.id


def test_freight_line_to_job_makes_expense_but_no_billing_row(tenant_db):
    job = _job(tenant_db)
    inv = _invoice_with_lines(tenant_db, matched_job_id=job.id)
    freight = inv.lines[2]

    confirm_line(tenant_db, inv, freight, disposition="job",
                 company_id=TID, actor_id="u1")

    assert tenant_db.query(Expense).count() == 1
    # freight is a cost, never a billable part
    assert tenant_db.query(JobPartNeeded).count() == 0


def test_job_line_without_any_job_raises(tenant_db):
    inv = _invoice_with_lines(tenant_db, matched_job_id=None)
    with pytest.raises(ConfirmError):
        confirm_line(tenant_db, inv, inv.lines[0], disposition="job",
                     company_id=TID, actor_id="u1")


def test_job_line_with_nonexistent_job_raises_confirm_error(tenant_db):
    """A bogus job id is a 400 (ConfirmError), not an FK 500 on flush."""
    from uuid import uuid4
    inv = _invoice_with_lines(tenant_db, matched_job_id=uuid4())  # not a real job
    with pytest.raises(ConfirmError):
        confirm_line(tenant_db, inv, inv.lines[0], disposition="job",
                     company_id=TID, actor_id="u1")


def test_confirm_is_idempotent(tenant_db):
    job = _job(tenant_db)
    inv = _invoice_with_lines(tenant_db, matched_job_id=job.id)
    line = inv.lines[0]

    confirm_line(tenant_db, inv, line, disposition="job", company_id=TID, actor_id="u1")
    res2 = confirm_line(tenant_db, inv, line, disposition="job", company_id=TID, actor_id="u1")

    assert res2.get("already_confirmed") is True
    # No double Expense / double billing row
    assert tenant_db.query(Expense).count() == 1
    assert tenant_db.query(JobPartNeeded).count() == 1


# --------------------------------------------------------------------------- #
# stock disposition
# --------------------------------------------------------------------------- #
def test_stock_line_increments_inventory_and_logs_adjustment(tenant_db):
    inv = _invoice_with_lines(tenant_db)
    item = InventoryItem(part_name="9x7 White Panel", quantity=5, unit_cost=Decimal("0"))
    tenant_db.add(item)
    tenant_db.flush()

    confirm_line(tenant_db, inv, inv.lines[0], disposition="stock",
                 company_id=TID, actor_id="u1",
                 inventory_item_id=item.id, update_catalog_cost=True)

    assert item.quantity == 7  # 5 + qty 2
    adj = tenant_db.query(StockAdjustment).one()
    assert adj.reason == "vendor_invoice"
    assert adj.quantity_delta == 2
    assert inv.lines[0].inventory_item_id == item.id
    assert inv.lines[0].stock_adjustment_id == adj.id
    # update_catalog_cost pushed the receipt cost onto the item
    assert item.unit_cost == Decimal("100.0000")
    # stock lines never create an Expense or billing row
    assert tenant_db.query(Expense).count() == 0
    assert tenant_db.query(JobPartNeeded).count() == 0


def test_stock_requires_inventory_item(tenant_db):
    inv = _invoice_with_lines(tenant_db)
    with pytest.raises(ConfirmError):
        confirm_line(tenant_db, inv, inv.lines[0], disposition="stock",
                     company_id=TID, actor_id="u1")


def test_freight_cannot_go_to_stock(tenant_db):
    inv = _invoice_with_lines(tenant_db)
    item = InventoryItem(part_name="x", quantity=1)
    tenant_db.add(item)
    tenant_db.flush()
    with pytest.raises(ConfirmError):
        confirm_line(tenant_db, inv, inv.lines[2], disposition="stock",
                     company_id=TID, actor_id="u1", inventory_item_id=item.id)


# --------------------------------------------------------------------------- #
# overhead + skip
# --------------------------------------------------------------------------- #
def test_overhead_makes_expense_without_job(tenant_db):
    inv = _invoice_with_lines(tenant_db)
    confirm_line(tenant_db, inv, inv.lines[0], disposition="overhead",
                 company_id=TID, actor_id="u1")
    exp = tenant_db.query(Expense).one()
    assert exp.source == "vendor_invoice"
    assert exp.category == "supplies"
    assert exp.job_id is None


def test_skip_requires_reason(tenant_db):
    inv = _invoice_with_lines(tenant_db)
    with pytest.raises(ConfirmError):
        confirm_line(tenant_db, inv, inv.lines[0], disposition="skip",
                     company_id=TID, actor_id="u1")

    confirm_line(tenant_db, inv, inv.lines[1], disposition="skip",
                 company_id=TID, actor_id="u1", skip_reason="already on the estimate")
    assert inv.lines[1].skip_reason == "already on the estimate"
    assert inv.lines[1].status == LINE_CONFIRMED
    assert tenant_db.query(Expense).count() == 0
