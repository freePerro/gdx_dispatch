"""MCP tool: customer.lookup — read a single customer by id."""
from __future__ import annotations

from typing import Any

from gdx_dispatch.core.mcp_registry import register_tool, require_capability
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor

DESCRIPTOR = ToolDescriptor(
    name="customer.lookup",
    description="Look up a customer by id. Returns the customer's visible fields subject to RLS + custom-field sensitivity.",
    input_schema={
        "type": "object",
        "required": ["customer_id"],
        "properties": {
            "customer_id": {"type": "string", "description": "Customer UUID"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "email": {"type": "string"},
            "phone": {"type": "string"},
        },
    },
    capabilities_required=[("read", "customer")],
    sensitivity_class="internal",
)


async def handler(
    *,
    customer_id: str,
    principal: Any = None,
    db: Any = None,
    **_ignored: Any,
) -> dict[str, Any]:
    """Return the customer record visible to ``principal``.

    Capability gate fires before any DB access — callers without
    ``read:customer`` cannot even confirm existence of a row. This is
    the "fail-loud, never-silent" contract all MCP tools honour.
    """
    require_capability(principal, DESCRIPTOR)
    if not customer_id or not isinstance(customer_id, str):
        raise ValueError("customer_id must be a non-empty string")

    # Real wiring: SS-19 transport adapter injects ``db`` (a tenant-
    # scoped Session) and resolves the Customer. Until SS-19 lands, the
    # handler returns an echo payload so the registry + schema checks
    # can be exercised end-to-end in tests.
    if db is None:
        return {"id": customer_id, "name": None, "email": None, "phone": None, "_stub": True}

    from gdx_dispatch.models.platform import Customer  # local import: avoid eager model pull

    row = db.get(Customer, customer_id)
    if row is None:
        return {"id": customer_id, "found": False}
    return {
        "id": str(row.id),
        "name": getattr(row, "name", None),
        "email": getattr(row, "email", None),
        "phone": getattr(row, "phone", None),
    }


register_tool(DESCRIPTOR, handler)
