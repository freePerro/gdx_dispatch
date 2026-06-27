"""MCP tool: catalog.list — list custom catalogs and/or items. Green."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.mcp_tools._helpers import coerce_uuid

DESCRIPTOR = ToolDescriptor(
    name="catalog.list",
    description=(
        "List custom catalogs. If catalog_id is provided, returns the items "
        "in that catalog instead. Default item limit 50, max 500."
    ),
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "catalog")],
    input_schema={
        "type": "object",
        "properties": {
            "catalog_id": {"type": ["string", "null"]},
            "active_only": {"type": "boolean", "default": True},
            "category": {"type": ["string", "null"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "catalogs": {"type": "array", "items": {"type": "object"}},
            "items": {"type": "array", "items": {"type": "object"}},
            "error": {"type": "string"},
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    catalog_id: str | None = None,
    active_only: bool = True,
    category: str | None = None,
    limit: int = 50,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import CustomCatalog, CustomCatalogItem

    lim = max(1, min(500, int(limit) if limit else 50))

    if catalog_id is None:
        stmt = (
            select(
                CustomCatalog,
                func.count(CustomCatalogItem.id).label("item_count"),
            )
            .outerjoin(
                CustomCatalogItem,
                (CustomCatalogItem.catalog_id == CustomCatalog.id)
                & (CustomCatalogItem.deleted_at.is_(None)),
            )
            .where(CustomCatalog.deleted_at.is_(None))
            .group_by(CustomCatalog.id)
            .order_by(CustomCatalog.name.asc())
            .limit(lim)
        )
        rows = list(db.execute(stmt))
        return {
            "catalogs": [
                {
                    "id": str(c.id),
                    "name": c.name,
                    "source_system": c.source_system,
                    "item_count": int(item_count or 0),
                }
                for c, item_count in rows
            ]
        }

    cid = coerce_uuid(catalog_id)
    if cid is None:
        return {"error": "invalid catalog_id"}

    catalog = db.get(CustomCatalog, cid)
    if catalog is None or catalog.deleted_at is not None:
        return {"error": "catalog not found"}

    item_stmt = (
        select(CustomCatalogItem)
        .where(CustomCatalogItem.catalog_id == cid)
        .where(CustomCatalogItem.deleted_at.is_(None))
    )
    if active_only:
        item_stmt = item_stmt.where(CustomCatalogItem.active.is_(True))
    if category:
        item_stmt = item_stmt.where(CustomCatalogItem.category == category)
    item_stmt = item_stmt.order_by(CustomCatalogItem.name.asc()).limit(lim)

    items = list(db.execute(item_stmt).scalars())
    return {
        "items": [
            {
                "id": str(i.id),
                "catalog_id": str(i.catalog_id),
                "sku": i.sku,
                "name": i.name,
                "description": i.description,
                "cost": float(i.cost or 0),
                "price": float(i.price or 0),
                "category": i.category,
                "pricing_category": i.pricing_category,
                "active": bool(i.active),
            }
            for i in items
        ]
    }


register_tool(DESCRIPTOR, handler)
