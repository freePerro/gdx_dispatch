from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool
from gdx_dispatch.models.tenant_models import Invoice

DESCRIPTOR = ToolDescriptor(
    name="customers.lifetime_analysis",
    description="Lifetime revenue rollup for one customer: total paid, invoice count, first/last invoice dates.",
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "customer"), ("read", "invoice")],
    input_schema={
        "type": "object",
        "required": ["customer_id"],
        "properties": {
            "customer_id": {"type": "string"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "lifetime": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "total_paid": {"type": "number"},
                    "invoice_count": {"type": "integer"},
                    "first_invoice_date": {"type": ["string", "null"]},
                    "last_invoice_date": {"type": ["string", "null"]},
                },
            },
        },
    },
)


def _iso(value: Any) -> str | None:
    if not value:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


async def handler(principal: Any, db: Any, customer_id: str, **_) -> dict[str, Any]:
    """
    Calculates lifetime revenue rollup for a specific customer.
    """
    # Sums `total`, NOT the dropped `total_amount` (migration 073) — that column
    # was NULL on every row, so lifetime value reported $0 for every customer.
    # An ORM select, not a SQL string: Session.execute refuses a bare string,
    # and the string also named `issue_date`, which invoices does not have
    # (the column is `invoice_date`), so it only ever ran against the unit
    # test's mock (GDXA-209). Binding through the Uuid column also matches on
    # SQLite, where a raw `customer_id = :cid` with a dashed id never does.
    try:
        cid = UUID(str(customer_id))
    except ValueError as exc:
        # Raised, not returned: invoke_tool maps a raise to execution_error,
        # but reports a returned {"ok": False} dict as a successful result.
        raise ValueError(f"customer_id is not a UUID: {customer_id!r}") from exc
    stmt = select(
        func.coalesce(func.sum(Invoice.total), 0),
        func.count(),
        func.min(Invoice.invoice_date),
        func.max(Invoice.invoice_date),
    ).where(
        Invoice.customer_id == cid,
        Invoice.status == "paid",
        Invoice.deleted_at.is_(None),
    )

    result = db.execute(stmt)
    row = result.first()

    if not row:
        # Fallback if no rows returned, though COALESCE/COUNT usually return a row.
        total_paid, count, first, last = 0.0, 0, None, None
    else:
        total_paid, count, first, last = row

    return {
        "lifetime": {
            "customer_id": str(customer_id),
            "total_paid": float(total_paid),
            "invoice_count": int(count),
            "first_invoice_date": _iso(first),
            "last_invoice_date": _iso(last),
        }
    }


register_tool(DESCRIPTOR, handler)
