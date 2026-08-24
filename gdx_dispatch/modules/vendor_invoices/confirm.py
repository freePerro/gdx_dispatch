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
)
from gdx_dispatch.modules.inventory.stock import apply_stock_delta
from gdx_dispatch.modules.vendor_invoices.models import (
    DISP_JOB,
    DISP_OVERHEAD,
    DISP_SKIP,
    DISP_STOCK,
    KIND_ITEM,
    LINE_CONFIRMED,
    LINE_PENDING,
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


def _post_expense_to_ledger(db: Session, expense: Expense) -> None:
    """GL symmetry (books-convergence Track 1): a vendor-bill expense posts
    P5 exactly like a manually keyed one. ``post_expense_recorded`` is
    flag-gated internally (no-op until ``ledger_posting_enabled``), and the
    flag-flip backfill sweeps ALL expenses regardless of source, so pre-flag
    rows are covered either way.

    Era guard (plan-audit MUST-FIX 9c): a bill dated before the GL cutover
    belongs to the QBO-era books — the backfill deliberately skips
    pre-cutover expenses, and posting one here would slam into the era lock
    at confirm time. Same by-DATE rule, applied symmetrically.

    Cash-basis timing (Doug, 2026-08-14: "payment date for cash basis" —
    resolves the 9b CPA flag): the expense date comes from
    ``payments.effective_expense_date`` — the settlement date when the bill
    is fully paid (for bank-match payments that's the literal bank date),
    else the invoice date as a placeholder that
    ``payments.sync_expense_dates`` re-dates + reposts when payment lands.
    """
    from gdx_dispatch.modules.ledger import service as ledger_service
    from gdx_dispatch.modules.ledger.rules import post_expense_recorded

    settings = ledger_service.get_gl_settings(db, expense.company_id)
    cutover = settings.cutover_month if settings else None
    if cutover is not None and expense.date and expense.date < cutover:
        return
    post_expense_recorded(db, expense)


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
    # Cash basis: a line confirmed on an ALREADY-settled bill dates its
    # expense at the settlement date, not the invoice date.
    from gdx_dispatch.modules.vendor_invoices.payments import effective_expense_date

    invoice_date = effective_expense_date(db, invoice)
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
        _post_expense_to_ledger(db, expense)
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
        adj = apply_stock_delta(
            db,
            item,
            delta=delta,
            reason="vendor_invoice",
            notes=f"Invoice {invoice.invoice_number} line {line.line_no}",
        )
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
        _post_expense_to_ledger(db, expense)
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



class LineBilledError(RuntimeError):
    """A confirmed line's checklist row was billed to a customer invoice —
    caught between the router's unlocked fast-path check and the
    authoritative locked read inside reverse_confirmed_line. The void must
    refuse: silently keeping (or deleting) a billed row breaks the customer
    invoice's provenance."""

    def __init__(self, line) -> None:
        self.line = line
        super().__init__(f"line {line.line_no} is billed to a customer invoice")


def reverse_confirmed_line(db: Session, invoice: VendorInvoice, line: VendorInvoiceLine, *, actor_id: str) -> dict:
    """Undo one confirmed line's effects so a bill void reverses instead of
    orphaning (money-audit M29: voiding a $3,120 bill left the Expense rows,
    the stock increment, and the checklist rows all standing, and the
    corrected re-issue then imported cleanly — the costs existed twice).

    Every effect is keyed on the line (expense_id / stock_adjustment_id /
    job_part_needed_id), so reversal is lookups of recorded artifacts, not
    re-derivations:

    - the Expense soft-deletes, and its LIVE ledger entry — when one EXISTS —
      reverses through the engine's idempotent reverse_entry. Existence, not
      the posting flag, is the predicate: an entry posted while the flag was
      ON must still unwind when the flag is OFF at void time. A reversal
      whose own month is locked posts into the current period instead (the
      same lock-tolerant escape hatch payments.py uses); if TODAY's period
      is locked too, PeriodLockedError propagates and the whole void rolls
      back — a half-reversed void would be this finding's own shape rebuilt,
      and the audit trail may never claim a reversal that did not post;
    - stock negates the STORED StockAdjustment.quantity_delta — the recorded
      artifact, not a re-derivation from line.quantity, which can have been
      edited since confirm;
    - the checklist row is re-read UNDER LOCK (populate_existing defeats the
      identity map; FOR UPDATE holds the row against billing's concurrent
      ``UPDATE … WHERE billed_invoice_id IS NULL`` claim): billed since the
      router's fast-path check → LineBilledError, the caller 409s and the
      transaction rolls back; unbilled → removed (this confirm minted it);
    - ``update_catalog_cost`` is deliberately NOT reversed — the old unit
      cost was never stored, and a cost observation is informational.

    The line returns to pending — routing keys, skip reason and confirm
    stamps all cleared — so a corrected re-issue starts clean.
    """
    from sqlalchemy import select as _select

    from gdx_dispatch.models.tenant_models import JobPartNeeded

    out = {"line_id": str(line.id), "expense_reversed": False,
           "ledger_reversed": False, "stock_reversed": False,
           "checklist_removed": False}

    if line.expense_id is not None:
        expense = db.get(Expense, line.expense_id)
        if expense is not None and expense.deleted_at is None:
            expense.deleted_at = _now()
            out["expense_reversed"] = True

            from gdx_dispatch.modules.ledger.engine import (
                PeriodLockedError,
                reverse_entry,
            )
            from gdx_dispatch.modules.ledger.rules import _live_expense_entry

            live = _live_expense_entry(db, expense)
            if live is not None:
                try:
                    reverse_entry(db, live, created_by=actor_id)
                except PeriodLockedError:
                    # The entry's own month is closed — post the reversal
                    # into the current period. If THAT is locked too, the
                    # error propagates: the caller refuses the whole void.
                    reverse_entry(
                        db, live,
                        effective_at=datetime.now(timezone.utc).date(),
                        created_by=actor_id,
                    )
                out["ledger_reversed"] = True

    if line.stock_adjustment_id is not None and line.inventory_item_id is not None:
        from gdx_dispatch.models.tenant_models import InventoryItem, StockAdjustment
        from gdx_dispatch.modules.inventory.stock import apply_stock_delta

        item = db.get(InventoryItem, line.inventory_item_id)
        if item is not None:
            adj = db.get(StockAdjustment, line.stock_adjustment_id)
            if adj is not None:
                delta = -int(adj.quantity_delta)
            else:
                # The recorded artifact is gone (should not happen — this
                # confirm stamped the FK). Re-derive rather than leave the
                # increment standing, and say so in the log.
                log.warning(
                    "vendor_void_stock_adjustment_missing line=%s adj=%s",
                    line.id, line.stock_adjustment_id,
                )
                delta = -_int_qty(line.quantity)
            apply_stock_delta(
                db, item, delta=delta,
                reason="vendor bill voided",
                notes=f"Reversal: invoice {invoice.invoice_number} line {line.line_no}",
            )
            out["stock_reversed"] = True

    if line.job_part_needed_id is not None:
        jpn = db.execute(
            _select(JobPartNeeded)
            .where(JobPartNeeded.id == line.job_part_needed_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if jpn is not None:
            if jpn.billed_invoice_id is not None:
                raise LineBilledError(line)
            db.delete(jpn)
            out["checklist_removed"] = True

    line.status = LINE_PENDING
    line.disposition = "pending"
    line.skip_reason = None
    line.job_id = None
    line.inventory_item_id = None
    line.expense_id = None
    line.stock_adjustment_id = None
    line.job_part_needed_id = None
    line.confirmed_by_user_id = None
    line.confirmed_at = None
    return out


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
