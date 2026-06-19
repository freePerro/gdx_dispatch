from __future__ import annotations

from typing import Any
from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool
from gdx_dispatch.models.tenant_models import Customer

DESCRIPTOR = ToolDescriptor(
    name="customers.detail",
    description="Lookup a single customer by ID and return their visible fields.",
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "customer")],
    input_schema={
        "type": "object",
        "properties": {
            "customer_id": {"type": "string"},
        },
        "required": ["customer_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "customer": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "status": {"type": "string", "enum": ["active", "inactive"]},
                },
                "nullable": True,
            },
        },
    },
)


async def handler(principal: Any, db: Any, customer_id: str, **_) -> dict[str, Any]:
    """
    Handler for customers.detail.
    Performs a synchronous primary-key lookup via db.get.
    """
    # The spec notes: row = db.get(Customer, customer_id) — sync, no await.
    row = db.get(Customer, customer_id)

    if row is None:
        return {"customer": None}

    return {
        "customer": {
            "id": str(row.id),
            "name": row.name,
            "email": row.email,
            "phone": row.phone,
            "status": "active" if row.deleted_at is None else "inactive",
        }
    }


register_tool(DESCRIPTOR, handler)
