"""MCP tool: catalog.get_item — fetch a single catalog item. Green."""
from __future__ import annotations

from typing import Any

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.mcp_tools._helpers import coerce_uuid

DESCRIPTOR = ToolDescriptor(
    name="catalog.get_item",
    description="Fetch a single custom catalog item by id.",
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "catalog")],
    input_schema={
        "type": "object",
        "required": ["item_id"],
        "properties": {
            "item_id": {"type": "string"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "item": {"type": "object"},
            "error": {"type": "string"},
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    item_id: str,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import CustomCatalogItem

    iid = coerce_uuid(item_id)
    if iid is None:
        return {"error": "invalid item_id"}

    item = db.get(CustomCatalogItem, iid)
    if item is None or item.deleted_at is not None:
        return {"error": "item not found"}

    return {
        "item": {
            "id": str(item.id),
            "catalog_id": str(item.catalog_id),
            "sku": item.sku,
            "name": item.name,
            "description": item.description,
            "cost": float(item.cost or 0),
            "price": float(item.price or 0),
            "category": item.category,
            "pricing_category": item.pricing_category,
            "active": bool(item.active),
            "qb_item_id": item.qb_item_id,
        }
    }


register_tool(DESCRIPTOR, handler)
