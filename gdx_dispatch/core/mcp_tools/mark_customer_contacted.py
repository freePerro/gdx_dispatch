from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.models.tenant_models import Customer


DESCRIPTOR = ToolDescriptor(
    name="customers.mark_contacted",
    description="Marks a customer as contacted by updating last_contacted_at and optionally appending a note.",
    blast_radius="green",
    approval_required=False,
    sensitivity_class="internal",
    capabilities_required=[("write", "customer.contact")],
    input_schema={
        "type": "object",
        "properties": {
            "customer_id": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["customer_id"],
        "additionalProperties": False,
    },
)


async def handler(
    db: Any,
    principal: Any,
    customer_id: str,
    note: str | None = None,
) -> dict[str, Any]:
    """
    Marks a customer as contacted.
    Updates last_contacted_at and appends to notes_appended if note is provided.
    """
    # db is a sync SQLAlchemy Session
    customer = db.get(Customer, customer_id)

    if customer is None:
        # The test allows either input_invalid or execution_error.
        # We'll raise an exception which invoke_tool will catch and turn into an error.
        raise ValueError(f"Customer with id {customer_id} not found.")

    # Update timestamp
    customer.last_contacted_at = datetime.now(timezone.utc)

    # Append note if provided
    if note:
        current_notes = customer.notes_appended or ""
        if current_notes:
            customer.notes_appended = f"{current_notes}\n\n{note}"
        else:
            customer.notes_appended = note

    db.commit()

    return {"ok": True}


register_tool(DESCRIPTOR, handler)
