from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool
from gdx_dispatch.models.tenant_models import Invoice


DESCRIPTOR = ToolDescriptor(
    name="invoices.list",
    description="List invoices with optional filters (status/customer_id/since).",
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "invoice")],
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["paid", "unpaid", "overdue", "all"],
            },
            "customer_id": {"type": "string"},
            "since": {"type": "string", "format": "date-time"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "invoices": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "invoice_number": {"type": "string"},
                        "customer_id": {"type": "string"},
                        "status": {"type": "string"},
                        "total_amount": {"type": "number"},
                        "amount_due": {"type": "number"},
                        "due_date": {"type": "string", "format": "date-time"},
                    },
                },
            },
            "truncated": {"type": "boolean"},
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    status: str | None = None,
    customer_id: str | None = None,
    since: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """List invoices with optional filters."""
    stmt = select(Invoice)

    if status == "paid":
        stmt = stmt.where(Invoice.status == "paid")
    elif status == "unpaid":
        stmt = stmt.where(Invoice.status != "paid")
    elif status == "overdue":
        stmt = stmt.where(Invoice.status == "overdue")

    if customer_id:
        stmt = stmt.where(Invoice.customer_id == customer_id)

    if since:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        stmt = stmt.where(Invoice.created_at >= since_dt)

    stmt = stmt.limit(51)
    result = db.execute(stmt)
    rows = result.scalars().all()

    truncated = len(rows) > 50
    invoices_to_return = rows[:50]

    def _f(value: Any) -> float | None:
        """Coerce Decimal/None/string to float; None passes through."""
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    invoices_data = []
    for inv in invoices_to_return:
        invoices_data.append({
            "id": str(inv.id),
            "invoice_number": inv.invoice_number,
            "customer_id": str(inv.customer_id),
            "status": inv.status,
            "total_amount": _f(inv.total_amount),
            "amount_due": _f(getattr(inv, "amount_due", None)),
            "due_date": inv.due_date.isoformat() if inv.due_date else None,
        })

    return {
        "invoices": invoices_data,
        "truncated": truncated,
    }


register_tool(DESCRIPTOR, handler)
