"""MCP tool: catalog.update_item — update a single catalog item. Yellow."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.mcp_tools._helpers import coerce_uuid

_PRICING_CATEGORIES = {"doors", "openers", "parts", "labor", "other"}


DESCRIPTOR = ToolDescriptor(
    name="catalog.update_item",
    description=(
        "Update a single catalog item. Yellow tool — preview on first call, "
        "confirm to apply. Any of name/sku/description/cost/price/category/"
        "pricing_category/active may be passed; omitted fields are unchanged."
    ),
    blast_radius="yellow",
    approval_required=True,
    sensitivity_class="internal",
    capabilities_required=[("write", "catalog")],
    input_schema={
        "type": "object",
        "required": ["item_id"],
        "properties": {
            "item_id": {"type": "string"},
            "name": {"type": ["string", "null"], "minLength": 1, "maxLength": 200},
            "sku": {"type": ["string", "null"], "maxLength": 100},
            "description": {"type": ["string", "null"]},
            "cost": {"type": ["number", "null"], "minimum": 0},
            "price": {"type": ["number", "null"], "minimum": 0},
            "category": {"type": ["string", "null"], "maxLength": 120},
            "pricing_category": {
                "type": ["string", "null"],
                "enum": [None, "doors", "openers", "parts", "labor", "other"],
            },
            "active": {"type": ["boolean", "null"]},
            "approval_ref": {"type": "string"},
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


_SENTINEL = object()


async def handler(
    principal: Any,
    db: Any,
    item_id: str,
    name: Any = _SENTINEL,
    sku: Any = _SENTINEL,
    description: Any = _SENTINEL,
    cost: Any = _SENTINEL,
    price: Any = _SENTINEL,
    category: Any = _SENTINEL,
    pricing_category: Any = _SENTINEL,
    active: Any = _SENTINEL,
    approval_ref: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import CustomCatalogItem

    iid = coerce_uuid(item_id)
    if iid is None:
        return {"error": "invalid item_id"}

    item = db.get(CustomCatalogItem, iid)
    if item is None or item.deleted_at is not None:
        return {"error": "item not found"}

    before = {
        "name": item.name,
        "sku": item.sku,
        "description": item.description,
        "cost": float(item.cost or 0),
        "price": float(item.price or 0),
        "category": item.category,
        "pricing_category": item.pricing_category,
        "active": bool(item.active),
    }

    new_name = item.name
    if name is not _SENTINEL:
        cleaned = (name or "").strip()
        if not cleaned:
            return {"error": "name must not be empty"}
        new_name = cleaned

    new_sku = item.sku
    if sku is not _SENTINEL:
        new_sku = (sku or "").strip() or None

    new_description = item.description if description is _SENTINEL else description
    new_category = item.category if category is _SENTINEL else category

    new_pricing_category = item.pricing_category
    if pricing_category is not _SENTINEL:
        if pricing_category is not None and pricing_category not in _PRICING_CATEGORIES:
            return {"error": f"pricing_category must be one of {sorted(_PRICING_CATEGORIES)}"}
        new_pricing_category = pricing_category

    new_cost = Decimal(str(item.cost or 0))
    if cost is not _SENTINEL:
        if cost is None or cost < 0:
            return {"error": "cost must be a number >= 0"}
        new_cost = Decimal(str(cost)).quantize(Decimal("0.01"))

    new_price = Decimal(str(item.price or 0))
    if price is not _SENTINEL:
        if price is None or price < 0:
            return {"error": "price must be a number >= 0"}
        new_price = Decimal(str(price)).quantize(Decimal("0.01"))

    new_active = bool(item.active) if active is _SENTINEL else bool(active)

    after = {
        "name": new_name,
        "sku": new_sku,
        "description": new_description,
        "cost": float(new_cost),
        "price": float(new_price),
        "category": new_category,
        "pricing_category": new_pricing_category,
        "active": new_active,
    }

    if not approval_ref:
        return {
            "item": {
                "preview": True,
                "id": str(item.id),
                "before": before,
                "after": after,
            }
        }

    item.name = new_name
    item.sku = new_sku
    item.description = new_description
    item.cost = new_cost
    item.price = new_price
    item.category = new_category
    item.pricing_category = new_pricing_category
    item.active = new_active
    db.commit()

    return {
        "item": {
            "preview": False,
            "id": str(item.id),
            **after,
        }
    }


register_tool(DESCRIPTOR, handler)
