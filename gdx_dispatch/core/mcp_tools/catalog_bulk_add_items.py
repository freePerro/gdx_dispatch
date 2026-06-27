"""MCP tool: catalog.bulk_add_items — add many items to one catalog. Yellow."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.mcp_tools._helpers import coerce_uuid

MAX_ITEMS = 500
_PRICING_CATEGORIES = {"doors", "openers", "parts", "labor", "other"}


_ITEM_SCHEMA = {
    "type": "object",
    "required": ["name"],
    "properties": {
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
    },
}


DESCRIPTOR = ToolDescriptor(
    name="catalog.bulk_add_items",
    description=(
        "Add multiple items to a single custom catalog. Yellow tool — first "
        "call returns a preview (count + sample + sku conflicts), second call "
        f"with approval_ref applies. Cap: {MAX_ITEMS} items per call. SKUs that "
        "already exist in this catalog are skipped (reported in 'skipped_skus')."
    ),
    blast_radius="yellow",
    approval_required=True,
    sensitivity_class="internal",
    capabilities_required=[("write", "catalog")],
    input_schema={
        "type": "object",
        "required": ["catalog_id", "items"],
        "properties": {
            "catalog_id": {"type": "string"},
            "items": {
                "type": "array",
                "items": _ITEM_SCHEMA,
                "minItems": 1,
                "maxItems": MAX_ITEMS,
            },
            "approval_ref": {"type": "string"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "result": {"type": "object"},
            "error": {"type": "string"},
        },
    },
)


def _normalize_item(idx: int, raw: dict) -> tuple[dict | None, str | None]:
    if not isinstance(raw, dict):
        return None, f"items[{idx}] must be an object"
    name = (raw.get("name") or "").strip()
    if not name:
        return None, f"items[{idx}].name must not be empty"
    if len(name) > 200:
        return None, f"items[{idx}].name exceeds 200 chars"

    sku_raw = raw.get("sku")
    sku = (sku_raw or "").strip() or None
    if sku is not None and len(sku) > 100:
        return None, f"items[{idx}].sku exceeds 100 chars"

    cost = raw.get("cost", 0) or 0
    price = raw.get("price", 0) or 0
    if cost < 0 or price < 0:
        return None, f"items[{idx}] cost/price must be >= 0"

    pricing_category = raw.get("pricing_category")
    if pricing_category is not None and pricing_category not in _PRICING_CATEGORIES:
        return None, (
            f"items[{idx}].pricing_category must be one of {sorted(_PRICING_CATEGORIES)}"
        )

    return (
        {
            "name": name,
            "sku": sku,
            "description": raw.get("description"),
            "cost": Decimal(str(cost)).quantize(Decimal("0.01")),
            "price": Decimal(str(price)).quantize(Decimal("0.01")),
            "category": raw.get("category"),
            "pricing_category": pricing_category,
            "active": bool(raw.get("active", True)),
        },
        None,
    )


async def handler(
    principal: Any,
    db: Any,
    catalog_id: str,
    items: list[dict],
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

    if not isinstance(items, list) or not items:
        return {"error": "items must be a non-empty list"}
    if len(items) > MAX_ITEMS:
        return {"error": f"too many items: {len(items)} > {MAX_ITEMS}"}

    normalized: list[dict] = []
    for idx, raw in enumerate(items):
        norm, err = _normalize_item(idx, raw)
        if err is not None:
            return {"error": err}
        normalized.append(norm)

    incoming_skus = [n["sku"] for n in normalized if n["sku"]]
    existing_skus: set[str] = set()
    if incoming_skus:
        rows = db.execute(
            select(CustomCatalogItem.sku)
            .where(CustomCatalogItem.catalog_id == cid)
            .where(CustomCatalogItem.deleted_at.is_(None))
            .where(CustomCatalogItem.sku.in_(incoming_skus))
        ).scalars()
        existing_skus = {s for s in rows if s}

    to_insert = [n for n in normalized if not n["sku"] or n["sku"] not in existing_skus]
    skipped = sorted(existing_skus)

    if not approval_ref:
        return {
            "result": {
                "preview": True,
                "catalog_id": str(cid),
                "catalog_name": catalog.name,
                "requested": len(normalized),
                "to_insert": len(to_insert),
                "skipped_skus": skipped,
                "sample": [
                    {
                        "name": n["name"],
                        "sku": n["sku"],
                        "cost": float(n["cost"]),
                        "price": float(n["price"]),
                    }
                    for n in to_insert[:10]
                ],
            }
        }

    inserted_ids: list[str] = []
    for n in to_insert:
        item = CustomCatalogItem(
            catalog_id=cid,
            sku=n["sku"],
            name=n["name"],
            description=n["description"],
            cost=n["cost"],
            price=n["price"],
            category=n["category"],
            pricing_category=n["pricing_category"],
            active=n["active"],
        )
        db.add(item)
        db.flush()
        inserted_ids.append(str(item.id))
    db.commit()

    return {
        "result": {
            "preview": False,
            "catalog_id": str(cid),
            "requested": len(normalized),
            "inserted": len(inserted_ids),
            "inserted_ids": inserted_ids,
            "skipped_skus": skipped,
        }
    }


register_tool(DESCRIPTOR, handler)
