"""What's on order — committed spend the supplier hasn't billed yet.

The whole point of capturing order confirmations. The supplier's order number
becomes their invoice number, so an order threads to what follows it:

    order confirmation  →  vendor invoice  →  statement line
         (committed)          (billed)          (owed, until paid)

An order with none of the latter two is money spent that GDX could not see at
all before: the confirmation arrives days-to-weeks ahead of the bill.

The status here is a READ, computed on demand rather than stored, because the
things it depends on arrive later and independently — writing a status column
would mean remembering to update it from two other ingest paths, and a stale
"not yet billed" is worse than no answer.

Deliberately NOT summed with the statement balance anywhere. Committed and owed
are different kinds of money: one is a quote that may still change, the other is
a debt. Adding them would overstate what is due.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.vendor_invoices.models import VendorInvoice
from gdx_dispatch.modules.vendor_orders.models import VendorOrder, VendorOrderLine
from gdx_dispatch.modules.vendor_statements.models import (
    VendorStatement,
    VendorStatementLine,
)

ZERO = Decimal("0.00")

STATUS_AWAITING_BILL = "awaiting_bill"   # ordered, nothing billed yet
STATUS_BILLED = "billed"                 # a bill exists on this order number
STATUS_ON_STATEMENT = "on_statement"     # on the CURRENT statement — still owed
STATUS_SETTLED = "settled"               # was on a statement, has dropped off

# Settled and billed orders accumulate forever; only un-billed ones are
# actionable. Every awaiting-bill order is always returned regardless of age —
# those are the point — and the finished ones are capped to the most recent few
# so the payload stays bounded. Truncation is REPORTED, never silent.
MAX_FINISHED_ITEMS = 40


@dataclass
class OnOrderLine:
    line_no: int
    description: str | None
    notes: str | None          # the door spec: "CHI 4283 12x10 Black Long Panel..."
    quantity: Decimal
    unit: str | None
    unit_cost: Decimal
    line_total: Decimal


@dataclass
class OnOrderItem:
    # The row's own id — the suggest/confirm endpoints address the order, not
    # its number, because a number is only unique per vendor.
    order_id: str
    order_number: str
    order_date: date | None
    ship_to: str | None
    customer_po: str | None
    lot_no: str | None
    estimated_total: Decimal
    line_count: int
    status: str
    billed_total: Decimal | None       # what the invoice actually said, if any
    variance: Decimal | None           # billed - estimated, when both are known
    matched_job_id: str | None = None  # set only by a human confirming
    lines: list[OnOrderLine] = field(default_factory=list)


@dataclass
class OnOrderSummary:
    vendor_name: str
    vendor_code: str | None
    awaiting_bill_count: int = 0
    awaiting_bill_total: Decimal = ZERO
    items: list[OnOrderItem] = field(default_factory=list)
    # How many finished orders were left out of `items` by the cap. Non-zero
    # means the list is partial — say so rather than letting it read complete.
    finished_truncated: int = 0


def build_on_order(db: Session) -> list[OnOrderSummary]:
    """Live orders per account, newest first, each threaded to what followed."""
    orders = db.execute(
        select(VendorOrder)
        .where(VendorOrder.deleted_at.is_(None))
        .order_by(VendorOrder.order_date.desc().nullslast(),
                  VendorOrder.created_at.desc())
    ).scalars().all()
    if not orders:
        return []

    numbers = [o.order_number for o in orders]

    # Both joins are scoped BY VENDOR. An invoice number is only unique per
    # supplier — `vendor_invoices` enforces uniqueness on (vendor_key,
    # invoice_number), and rung 2 fills that table with any supplier's bills —
    # so matching on the bare number lets an unrelated vendor's invoice mark
    # this order billed. That silently drops it out of committed spend and
    # prints a nonsense variance, which is worse than showing nothing.
    vendors = {o.vendor_name for o in orders}

    billed: dict[tuple[str, str], Decimal] = {
        (row.vendor_name_raw, row.invoice_number): row.total
        for row in db.execute(
            select(VendorInvoice)
            .where(VendorInvoice.invoice_number.in_(numbers))
            .where(VendorInvoice.vendor_name_raw.in_(vendors))
            .where(VendorInvoice.deleted_at.is_(None))
        ).scalars().all()
    }

    # "Still being asked for" means present on the LATEST statement, not on any
    # statement ever — the same rule the account view runs on. An order that was
    # on an older statement and has since dropped off has been settled.
    latest_by_vendor: dict[str, object] = {}
    for st in db.execute(
        select(VendorStatement)
        .where(VendorStatement.deleted_at.is_(None))
        .where(VendorStatement.vendor_name.in_(vendors))
        .order_by(VendorStatement.statement_date.desc().nullslast(),
                  VendorStatement.created_at.desc())
    ).scalars().all():
        latest_by_vendor.setdefault(st.vendor_name, st)

    on_latest_statement: set[tuple[str, str]] = set()
    ever_on_statement: set[tuple[str, str]] = set()
    for vendor_name, st in latest_by_vendor.items():
        for (number,) in db.execute(
            select(VendorStatementLine.vendor_invoice_no)
            .where(VendorStatementLine.statement_id == st.id)
            .where(VendorStatementLine.vendor_invoice_no.in_(numbers))
        ).all():
            on_latest_statement.add((vendor_name, number))
    for (vendor_name, number) in db.execute(
        select(VendorStatement.vendor_name, VendorStatementLine.vendor_invoice_no)
        .join(VendorStatement, VendorStatement.id == VendorStatementLine.statement_id)
        .where(VendorStatement.deleted_at.is_(None))
        .where(VendorStatement.vendor_name.in_(vendors))
        .where(VendorStatementLine.vendor_invoice_no.in_(numbers))
    ).all():
        ever_on_statement.add((vendor_name, number))

    lines_by_order: dict = {}
    for ln in db.execute(
        select(VendorOrderLine)
        .where(VendorOrderLine.order_id.in_([o.id for o in orders]))
        .order_by(VendorOrderLine.line_no.asc())
    ).scalars().all():
        lines_by_order.setdefault(ln.order_id, []).append(ln)

    grouped: dict[tuple[str, str | None], OnOrderSummary] = {}
    for o in orders:
        key = (o.vendor_name, o.vendor_code)
        summary = grouped.setdefault(
            key, OnOrderSummary(vendor_name=o.vendor_name, vendor_code=o.vendor_code)
        )

        vkey = (o.vendor_name, o.order_number)
        billed_total = billed.get(vkey)
        if vkey in on_latest_statement:
            status = STATUS_ON_STATEMENT
        elif vkey in ever_on_statement:
            # Was on a statement, isn't on the current one — the supplier has
            # stopped asking, i.e. it's been paid. Without this an order stays
            # "the supplier wants this" forever and the list never drains.
            status = STATUS_SETTLED
        elif billed_total is not None:
            status = STATUS_BILLED
        else:
            status = STATUS_AWAITING_BILL

        item = OnOrderItem(
            order_id=str(o.id),
            order_number=o.order_number,
            order_date=o.order_date,
            ship_to=o.ship_to,
            customer_po=o.customer_po,
            lot_no=o.lot_no,
            estimated_total=o.estimated_total or ZERO,
            line_count=o.line_count,
            status=status,
            billed_total=billed_total,
            # Positive = billed for more than the order estimated. Freight added
            # at ship time is the usual innocent cause; it is surfaced, not
            # judged, because only the office knows which is which.
            variance=(billed_total - (o.estimated_total or ZERO)
                      if billed_total is not None else None),
            matched_job_id=str(o.matched_job_id) if o.matched_job_id else None,
            lines=[
                OnOrderLine(
                    line_no=ln.line_no,
                    description=ln.description,
                    notes=ln.notes,
                    quantity=ln.quantity or ZERO,
                    unit=ln.unit,
                    unit_cost=ln.unit_cost or ZERO,
                    line_total=ln.line_total or ZERO,
                )
                for ln in lines_by_order.get(o.id, [])
            ],
        )
        summary.items.append(item)
        if status == STATUS_AWAITING_BILL:
            summary.awaiting_bill_count += 1
            summary.awaiting_bill_total += item.estimated_total

    for summary in grouped.values():
        awaiting = [i for i in summary.items if i.status == STATUS_AWAITING_BILL]
        finished = [i for i in summary.items if i.status != STATUS_AWAITING_BILL]
        if len(finished) > MAX_FINISHED_ITEMS:
            summary.finished_truncated = len(finished) - MAX_FINISHED_ITEMS
            finished = finished[:MAX_FINISHED_ITEMS]
        summary.items = awaiting + finished

    return sorted(grouped.values(), key=lambda s: s.awaiting_bill_total, reverse=True)
