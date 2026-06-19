from __future__ import annotations

from typing import Any
from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool


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


async def handler(principal: Any, db: Any, customer_id: str, **_) -> dict[str, Any]:
    """
    Calculates lifetime revenue rollup for a specific customer.
    """
    # The query as specified in the requirements.
    # Note: The requirement says 'status=paid' and 'deleted_at IS NULL'.
    query = """
        SELECT
            COALESCE(SUM(total_amount), 0),
            COUNT(*),
            MIN(issue_date),
            MAX(issue_date)
        FROM invoices
        WHERE customer_id = :cid
          AND status = 'paid'
          AND deleted_at IS NULL
    """

    result = db.execute(query, {"cid": customer_id})
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
            "first_invoice_date": first if first else None,
            "last_invoice_date": last if last else None,
        }
    }


register_tool(DESCRIPTOR, handler)
