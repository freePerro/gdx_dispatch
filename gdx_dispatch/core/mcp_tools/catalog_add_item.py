"""MCP tool: catalog.add_item — add a single item to a custom catalog. Yellow."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.mcp_tools._helpers import coerce_uuid

_PRICING_CATEGORIES = {"doors", "openers", "parts", "labor", "other"}


DESCRIPTOR = ToolDescriptor(
    name="catalog.add_item",
    description=(
        "Add a single item to a custom catalog. Yellow tool — preview on "
        "first call, confirm to apply. If an item with the same SKU already "
        "exists in this catalog, returns an error (use catalog.update_item)."
    ),
    blast_radius="yellow",
    approval_required=True,
    sensitivity_class="internal",
    capabilities_required=[("write", "catalog")],
    input_schema={
        "type": "object",
        "required": ["catalog_id", "name"],
        "properties": {
            "catalog_id": {"type": "string"},
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "sku": {"type": ["string", "null"], "maxLength": 100},
            "description": {"type": ["string", "null"]},
            "cost": {"type": "number", "minimum": 0, "default": 0},
            "price": {"type": "number", "minimum": 0, "default": 0},
            "category": {"type": ["string", "null"], "maxLength": 120},
            "pricing_category": {
                "type": ["string", "null"],
                "enum": [None, "doors", "openers", "parts", "labor", "other"],
            },
            "active": {"type": "boolean", "default": True},
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


async def handler(
    principal: Any,
    db: Any,
    catalog_id: str,
    name: str,
    sku: str | None = None,
    description: str | None = None,
    cost: float = 0,
    price: float = 0,
    category: str | None = None,
    pricing_category: str | None = None,
    active: bool = True,
    approval_ref: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import CustomCatalog, CustomCatalogItem

    cid = coerce_uuid(catalog_id)
    if cid is None:
        return {"error": "invalid catalog_id"}

    catalog = db.get(CustomCatalog, cid)
    if catalog is None or catalog.deleted_at is not None:
        return {"error": "catalog not found"}

    cleaned_name = (name or "").strip()
    if not cleaned_name:
        return {"error": "name must not be empty"}
    if len(cleaned_name) > 200:
        return {"error": "name exceeds 200 chars"}

    cleaned_sku = (sku or "").strip() or None
    if pricing_category is not None and pricing_category not in _PRICING_CATEGORIES:
        return {"error": f"pricing_category must be one of {sorted(_PRICING_CATEGORIES)}"}
    if cost < 0 or price < 0:
        return {"error": "cost/price must be >= 0"}

    if cleaned_sku is not None:
        existing = db.execute(
            select(CustomCatalogItem)
            .where(CustomCatalogItem.catalog_id == cid)
            .where(CustomCatalogItem.sku == cleaned_sku)
            .where(CustomCatalogItem.deleted_at.is_(None))
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return {
                "error": (
                    f"sku {cleaned_sku!r} already exists in this catalog "
                    f"(item_id={existing.id}); use catalog.update_item"
                )
            }

    cost_d = Decimal(str(cost)).quantize(Decimal("0.01"))
    price_d = Decimal(str(price)).quantize(Decimal("0.01"))

    if not approval_ref:
        return {
            "item": {
                "preview": True,
                "catalog_id": str(cid),
                "catalog_name": catalog.name,
                "name": cleaned_name,
                "sku": cleaned_sku,
                "cost": float(cost_d),
                "price": float(price_d),
                "category": category,
                "pricing_category": pricing_category,
                "active": bool(active),
            }
        }

    item = CustomCatalogItem(
        catalog_id=cid,
        sku=cleaned_sku,
        name=cleaned_name,
        description=description,
        cost=cost_d,
        price=price_d,
        category=category,
        pricing_category=pricing_category,
        active=bool(active),
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    return {
        "item": {
            "preview": False,
            "id": str(item.id),
            "catalog_id": str(item.catalog_id),
            "name": item.name,
            "sku": item.sku,
            "cost": float(item.cost),
            "price": float(item.price),
            "category": item.category,
            "pricing_category": item.pricing_category,
            "active": bool(item.active),
        }
    }


register_tool(DESCRIPTOR, handler)
