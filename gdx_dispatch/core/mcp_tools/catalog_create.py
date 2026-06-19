"""MCP tool: catalog.create — create a new (empty) custom catalog. Green."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


DESCRIPTOR = ToolDescriptor(
    name="catalog.create",
    description=(
        "Create a new (empty) custom catalog. Returns the catalog id. If a "
        "non-deleted catalog with the same name already exists, returns it "
        "instead of creating a duplicate."
    ),
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("write", "catalog")],
    input_schema={
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "source_system": {"type": "string", "maxLength": 60, "default": "manual"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "catalog": {"type": "object"},
            "error": {"type": "string"},
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    name: str,
    source_system: str = "manual",
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import CustomCatalog

    cleaned = (name or "").strip()
    if not cleaned:
        return {"error": "name must not be empty"}
    if len(cleaned) > 200:
        return {"error": "name exceeds 200 chars"}

    src = (source_system or "manual").strip()[:60] or "manual"

    existing = db.execute(
        select(CustomCatalog)
        .where(CustomCatalog.name == cleaned)
        .where(CustomCatalog.deleted_at.is_(None))
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return {
            "catalog": {
                "id": str(existing.id),
                "name": existing.name,
                "source_system": existing.source_system,
                "reused": True,
            }
        }

    catalog = CustomCatalog(name=cleaned, source_system=src)
    db.add(catalog)
    db.commit()
    db.refresh(catalog)

    return {
        "catalog": {
            "id": str(catalog.id),
            "name": catalog.name,
            "source_system": catalog.source_system,
            "reused": False,
        }
    }


register_tool(DESCRIPTOR, handler)
