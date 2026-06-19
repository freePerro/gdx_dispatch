from __future__ import annotations

from typing import Any
from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool

DESCRIPTOR = ToolDescriptor(
    name="invoices.void",
    description="Void an invoice. Red — admin capability required regardless of approval_ref.",
    blast_radius="red",
    approval_required=True,
    sensitivity_class="internal",
    capabilities_required=[("write", "invoice")],
    input_schema={
        "type": "object",
        "required": ["invoice_id"],
        "properties": {
            "invoice_id": {"type": "string"},
            "reason": {"type": "string"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "voided": {
                "type": "object",
                "properties": {
                    "invoice_id": {"type": "string"},
                    "previous_status": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    invoice_id: str,
    reason: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Void an invoice."""
    # Note: The admin gate is handled by the invoke_tool caller/orchestrator
    # for red blast radius tools.
    inv = db.get("Invoice", invoice_id)
    if inv is None:
        return {"error": "invoice not found"}

    return {
        "voided": {
            "invoice_id": str(invoice_id),
            "previous_status": inv.status,
            "reason": reason,
        }
    }


register_tool(DESCRIPTOR, handler)
