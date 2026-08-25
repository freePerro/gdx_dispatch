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


# ---------------------------------------------------------------------------
# fulfils_part_id — the office states which part a bill line paid for.
#
# This is the whole mechanism behind job costing's actual-vs-estimate split.
# A bill line carries no SKU, only the vendor's free text, so which part it
# paid for CANNOT be inferred — and inferring it by name is what AUDIT-R1
# forbids (core/part_pricing.py). Two earlier attempts at parts cost were
# pulled for trying. This makes the link explicit.
# ---------------------------------------------------------------------------


def _open_part(tenant_db, job, name="MODEL-A 9x7 White Panel", status="used", qty=2):
    row = JobPartNeeded(
        id=f"pn-{status}-{name[:6]}-{qty}".replace(" ", ""),
        company_id=TID, job_id=str(job.id), part_name=name,
        quantity=qty, status=status, source="closeout",
    )
    tenant_db.add(row)
    tenant_db.commit()
    return row


def test_fulfils_part_id_links_the_line_to_the_existing_part(tenant_db):
    job = _job(tenant_db)
    part = _open_part(tenant_db, job)
    inv = _invoice_with_lines(tenant_db, matched_job_id=job.id)
    item_line = inv.lines[0]  # hold the reference; ordering is not stable post-commit

    res = confirm_line(tenant_db, inv, item_line, disposition="job",
                       company_id=TID, actor_id="u1", fulfils_part_id=part.id)
    tenant_db.commit()

    assert res["linked_existing_part"] is True
    assert item_line.job_part_needed_id == part.id
    assert tenant_db.query(JobPartNeeded).count() == 1, "no new row is minted"
    tenant_db.refresh(part)
    assert part.status == "used", "a consumption fact is not rewritten"
    assert "90000001" in (part.notes or ""), "provenance recorded"


def test_without_fulfils_part_id_the_old_behaviour_is_unchanged(tenant_db):
    """Counterfactual: anyone not using the new control sees exactly what they
    saw before — a minted per-event row."""
    job = _job(tenant_db)
    part = _open_part(tenant_db, job)
    inv = _invoice_with_lines(tenant_db, matched_job_id=job.id)

    item_line = inv.lines[0]
    res = confirm_line(tenant_db, inv, item_line, disposition="job",
                       company_id=TID, actor_id="u1")
    tenant_db.commit()

    assert res["linked_existing_part"] is False
    assert tenant_db.query(JobPartNeeded).count() == 2, "the event log gains a row"
    assert item_line.job_part_needed_id != part.id
    tenant_db.refresh(part)
    assert part.status == "used", "the tech's row is untouched"


def test_a_part_from_another_job_is_refused(tenant_db):
    """The link is scoped to the job being billed — otherwise the office could
    attach a bill to a part on someone else's job and move real money."""
    job = _job(tenant_db)
    other = Job(title="Other job", company_id=TID)
    tenant_db.add(other)
    tenant_db.flush()
    stray = JobPartNeeded(id="pn-stray", company_id=TID, job_id=str(other.id),
                          part_name="MODEL-A 9x7 White Panel", quantity=1,
                          status="used", source="closeout")
    tenant_db.add(stray)
    tenant_db.commit()

    inv = _invoice_with_lines(tenant_db, matched_job_id=job.id)
    with pytest.raises(ConfirmError):
        confirm_line(tenant_db, inv, inv.lines[0], disposition="job",
                     company_id=TID, actor_id="u1", fulfils_part_id="pn-stray")


def test_an_unknown_part_id_is_refused(tenant_db):
    job = _job(tenant_db)
    inv = _invoice_with_lines(tenant_db, matched_job_id=job.id)
    with pytest.raises(ConfirmError):
        confirm_line(tenant_db, inv, inv.lines[0], disposition="job",
                     company_id=TID, actor_id="u1", fulfils_part_id="does-not-exist")


def test_a_freight_line_ignores_fulfils_part_id(tenant_db):
    """Freight is a cost but not a part; it must not consume the link."""
    job = _job(tenant_db)
    part = _open_part(tenant_db, job)
    inv = _invoice_with_lines(tenant_db, matched_job_id=job.id)
    freight = next(ln for ln in inv.lines if ln.kind != KIND_ITEM)

    confirm_line(tenant_db, inv, freight, disposition="job",
                 company_id=TID, actor_id="u1", fulfils_part_id=part.id)
    tenant_db.commit()

    assert freight.job_part_needed_id is None
    tenant_db.refresh(part)
    assert "90000001" not in (part.notes or "")


def test_voiding_a_LINKED_line_unlinks_but_never_deletes_the_techs_row(tenant_db):
    """Data-destruction guard.

    `reverse_confirmed_line` deletes whatever `job_part_needed_id` points at —
    correct while that was always a row confirm minted, catastrophic once
    `fulfils_part_id` can point it at the TECH'S attested closeout row (its sku,
    quantity, price_source, photo, requester). Voiding a mis-keyed bill would
    have destroyed evidence the bill never owned, and taken the job's parts cost
    to $0 with it.
    """
    from gdx_dispatch.modules.vendor_invoices.confirm import reverse_confirmed_line

    job = _job(tenant_db)
    part = _open_part(tenant_db, job)
    inv = _invoice_with_lines(tenant_db, matched_job_id=job.id)
    item_line = inv.lines[0]

    confirm_line(tenant_db, inv, item_line, disposition="job",
                 company_id=TID, actor_id="u1", fulfils_part_id=part.id)
    tenant_db.commit()

    out = reverse_confirmed_line(tenant_db, inv, item_line, actor_id="u1")
    tenant_db.commit()

    assert out.get("unlinked_existing_part") is True
    assert out.get("checklist_removed") is False
    survivor = tenant_db.query(JobPartNeeded).filter_by(id=part.id).one_or_none()
    assert survivor is not None, "the tech's consumption record must survive a bill void"
    assert survivor.status == "used"
    assert "90000001" not in (survivor.notes or ""), "provenance is stripped on unlink"
    assert item_line.job_part_needed_id is None


def test_voiding_a_MINTED_line_still_removes_its_row(tenant_db):
    """Counterfactual: the unchanged path must stay unchanged. A row this
    confirm created has no life of its own and still goes."""
    from gdx_dispatch.modules.vendor_invoices.confirm import reverse_confirmed_line

    job = _job(tenant_db)
    inv = _invoice_with_lines(tenant_db, matched_job_id=job.id)
    item_line = inv.lines[0]

    confirm_line(tenant_db, inv, item_line, disposition="job",
                 company_id=TID, actor_id="u1")
    tenant_db.commit()
    assert tenant_db.query(JobPartNeeded).count() == 1

    out = reverse_confirmed_line(tenant_db, inv, item_line, actor_id="u1")
    tenant_db.commit()

    assert out.get("checklist_removed") is True
    assert tenant_db.query(JobPartNeeded).count() == 0


def test_cannot_link_a_line_to_a_row_another_bill_minted(tenant_db):
    """`source` is the ownership record the void path relies on. If two lines
    could point at one minted row, reversing either would delete a row the
    other still references — a dangling id whose own reverse silently no-ops."""
    job = _job(tenant_db)
    inv = _invoice_with_lines(tenant_db, matched_job_id=job.id)
    first = inv.lines[0]

    confirm_line(tenant_db, inv, first, disposition="job",
                 company_id=TID, actor_id="u1")
    tenant_db.commit()
    minted = tenant_db.query(JobPartNeeded).one()
    assert minted.source == "vendor_invoice"

    inv2 = VendorInvoice(
        vendor_name_raw="Midwest Wholesale Doors", invoice_number="90000002",
        invoice_date=date(2026, 6, 30), subtotal=Decimal("100.00"),
        tax=Decimal("0.00"), shipping=Decimal("0.00"), total=Decimal("100.00"),
        matched_job_id=job.id,
    )
    inv2.lines = [
        VendorInvoiceLine(
            line_no=0, kind=KIND_ITEM, item_label="Another panel",
            description="MODEL-A 9x7 White Panel", quantity=Decimal("1"),
            unit_cost=Decimal("100.0000"), line_total=Decimal("100.00"),
        )
    ]
    tenant_db.add(inv2)
    tenant_db.flush()

    with pytest.raises(ConfirmError):
        confirm_line(tenant_db, inv2, inv2.lines[0], disposition="job",
                     company_id=TID, actor_id="u1", fulfils_part_id=minted.id)
