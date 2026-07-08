"""Vendor invoice line confirmation — the effects layer.

On confirm, a routed line produces exactly the downstream records the design
promises, and NOTHING before a human confirms:

- ``job``      → Expense(source='vendor_invoice') on the job (feeds costing)
               + one per-event JobPartNeeded(source='vendor_invoice',
                 status='received') on the billing spine (item lines only)
               + attaches the Document to the job.
- ``stock``    → InventoryItem.quantity increment + StockAdjustment
                 (the office-visible ledger, same as receive_po) + optional
                 catalog-cost update.
- ``overhead`` → Expense(source='vendor_invoice') with no job_id.
- ``skip``     → no effects; requires a reason.

Confirmation is idempotent: a line already ``confirmed`` is a no-op (guards
against double Expense / double stock increment on a retry).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import (
    Document,
    Expense,
    InventoryItem,
    Job,
    JobPartNeeded,
    StockAdjustment,
)
from gdx_dispatch.modules.vendor_invoices.models import (
    DISP_JOB,
    DISP_OVERHEAD,
    DISP_SKIP,
    DISP_STOCK,
    KIND_ITEM,
    LINE_CONFIRMED,
    VALID_DISPOSITIONS,
    VendorInvoice,
    VendorInvoiceLine,
)

log = logging.getLogger(__name__)

EXPENSE_SOURCE = "vendor_invoice"


class ConfirmError(ValueError):
    """Raised when a confirm request is invalid (missing target, bad reason)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _int_qty(qty: Decimal) -> int:
    # Inventory quantities are integers; truncate fractional coverage.
    return int(qty)


def confirm_line(
    db: Session,
    invoice: VendorInvoice,
    line: VendorInvoiceLine,
    *,
    disposition: str,
    company_id: str,
    actor_id: str,
    job_id: UUID | None = None,
    inventory_item_id: UUID | None = None,
    skip_reason: str | None = None,
    update_catalog_cost: bool = False,
) -> dict:
    """Confirm one line, applying its disposition's effects. Idempotent AND
    concurrency-safe: the line row is locked FOR UPDATE before the status
    check, so two concurrent confirms (double-click, client retry) serialize —
    the second sees ``confirmed`` and no-ops instead of doubling the Expense /
    stock increment. On Postgres this is a real row lock; on SQLite (tests) it
    degrades to a plain refresh, which is fine because those runs are
    single-threaded."""
    if disposition not in VALID_DISPOSITIONS or disposition == "pending":
        raise ConfirmError(f"invalid disposition {disposition!r}")

    # Fast in-session guard (a retry within the same unit of work).
    if line.status == LINE_CONFIRMED:
        return {"line_id": str(line.id), "already_confirmed": True}
    # Cross-transaction guard: lock the row and re-read the COMMITTED status so
    # a concurrent double-submit (double-click, client retry) serializes — the
    # second confirm blocks here until the first commits, then sees 'confirmed'
    # and no-ops instead of doubling the Expense / stock increment. Postgres
    # honors FOR UPDATE; SQLite (tests) degrades to a plain refresh, fine since
    # those runs are single-threaded.
    db.refresh(line, with_for_update=True)
    if line.status == LINE_CONFIRMED:
        return {"line_id": str(line.id), "already_confirmed": True}

    vendor_name = invoice.vendor_name_raw
    invoice_date = invoice.invoice_date or _now().date()
    result: dict = {"line_id": str(line.id), "disposition": disposition}

    if disposition == DISP_JOB:
        eff_job = job_id or invoice.matched_job_id
        if eff_job is None:
            raise ConfirmError("job disposition requires a job_id (none matched)")
        eff_job = _as_uuid(eff_job)
        # Validate the job exists so a bogus id is a 400, not a FK 500 on flush.
        if db.get(Job, eff_job) is None:
            raise ConfirmError(f"job {eff_job} not found")

        expense = Expense(
            company_id=company_id,
            vendor=vendor_name,
            amount=line.line_total,
            date=invoice_date,
            category="materials",
            description=line.description,
            job_id=eff_job,
            source=EXPENSE_SOURCE,
        )
        db.add(expense)
        db.flush()
        line.expense_id = expense.id
        result["expense_id"] = str(expense.id)

        # Billing spine — item lines only. Freight/tax are costs, not billable
        # parts, so they never become a JobPartNeeded checklist row.
        if line.kind == KIND_ITEM:
            jpn_id = str(uuid4())
            db.add(
                JobPartNeeded(
                    id=jpn_id,
                    company_id=company_id,
                    job_id=str(eff_job),
                    part_name=line.description[:200],
                    quantity=_int_qty(line.quantity),
                    supplier=vendor_name,
                    status="received",
                    source=EXPENSE_SOURCE,
                    unit_price=None,  # office prices it on the invoice
                    notes=f"From vendor invoice {invoice.invoice_number}",
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
            db.flush()  # don't rely on the caller's autoflush setting
            line.job_part_needed_id = jpn_id
            result["job_part_needed_id"] = jpn_id

        line.job_id = eff_job
        _attach_document_to_job(db, invoice, eff_job)

    elif disposition == DISP_STOCK:
        if line.kind != KIND_ITEM:
            raise ConfirmError("only item lines can be received into stock")
        if inventory_item_id is None:
            raise ConfirmError("stock disposition requires an inventory_item_id")
        item = db.get(InventoryItem, _as_uuid(inventory_item_id))
        if item is None:
            raise ConfirmError(f"inventory item {inventory_item_id} not found")

        delta = _int_qty(line.quantity)
        item.quantity = (item.quantity or 0) + delta
        adj = StockAdjustment(
            item_id=item.id,
            quantity_delta=delta,
            reason="vendor_invoice",
            notes=f"Invoice {invoice.invoice_number} line {line.line_no}",
        )
        db.add(adj)
        db.flush()
        line.inventory_item_id = item.id
        line.stock_adjustment_id = adj.id
        if update_catalog_cost:
            item.unit_cost = line.unit_cost
            result["catalog_cost_updated"] = True
        result["inventory_item_id"] = str(item.id)
        result["quantity_delta"] = delta

    elif disposition == DISP_OVERHEAD:
        expense = Expense(
            company_id=company_id,
            vendor=vendor_name,
            amount=line.line_total,
            date=invoice_date,
            category="supplies",
            description=line.description,
            job_id=None,
            source=EXPENSE_SOURCE,
        )
        db.add(expense)
        db.flush()
        line.expense_id = expense.id
        result["expense_id"] = str(expense.id)

    elif disposition == DISP_SKIP:
        if not (skip_reason and skip_reason.strip()):
            raise ConfirmError("skip disposition requires a reason")
        line.skip_reason = skip_reason.strip()

    line.disposition = disposition
    line.status = LINE_CONFIRMED
    line.confirmed_by_user_id = actor_id
    line.confirmed_at = _now()
    return result


def _attach_document_to_job(db: Session, invoice: VendorInvoice, job_id: UUID) -> None:
    if invoice.document_id is None:
        return
    doc = db.get(Document, invoice.document_id)
    if doc is not None and doc.job_id is None:
        doc.job_id = job_id


def maybe_mark_reviewed(db: Session, invoice: VendorInvoice, actor_id: str) -> bool:
    """If every line is confirmed, stamp the invoice reviewed. Returns whether
    it flipped."""
    if invoice.reviewed_at is not None:
        return False
    if invoice.lines and all(ln.status == LINE_CONFIRMED for ln in invoice.lines):
        invoice.reviewed_at = _now()
        invoice.reviewed_by_user_id = actor_id
        return True
    return False


def _as_uuid(value) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))
