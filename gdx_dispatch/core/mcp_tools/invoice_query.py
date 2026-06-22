"""MCP tool: invoice.query — list invoices matching criteria."""
from __future__ import annotations

from typing import Any

from gdx_dispatch.core.mcp_registry import register_tool, require_capability
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor

DESCRIPTOR = ToolDescriptor(
    name="invoice.query",
    description="Query invoices visible to the caller, optionally filtered by customer, status, or date range.",
    input_schema={
        "type": "object",
        "properties": {
            "customer_id": {"type": "string"},
            "status": {"type": "string", "enum": ["draft", "sent", "paid", "overdue", "void"]},
            "date_from": {"type": "string", "format": "date"},
            "date_to": {"type": "string", "format": "date"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "invoices": {"type": "array", "items": {"type": "object"}},
            "count": {"type": "integer"},
        },
    },
    capabilities_required=[("read", "invoice")],
    sensitivity_class="internal",
)


async def handler(
    *,
    customer_id: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    principal: Any = None,
    db: Any = None,
    **_ignored: Any,
) -> dict[str, Any]:
    require_capability(principal, DESCRIPTOR)
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")

    if db is None:
        return {"invoices": [], "count": 0, "_stub": True}

    # Real implementation wired in SS-19.
    from gdx_dispatch.models.tenant_models import Invoice  # local import

    q = db.query(Invoice)
    if customer_id:
        q = q.filter(Invoice.customer_id == customer_id)
    if status:
        q = q.filter(Invoice.status == status)
    if date_from:
        q = q.filter(Invoice.issued_at >= date_from)
    if date_to:
        q = q.filter(Invoice.issued_at <= date_to)
    rows = q.limit(limit).all()
    return {
        "invoices": [
            {"id": str(r.id), "status": getattr(r, "status", None)} for r in rows
        ],
        "count": len(rows),
    }


register_tool(DESCRIPTOR, handler)
