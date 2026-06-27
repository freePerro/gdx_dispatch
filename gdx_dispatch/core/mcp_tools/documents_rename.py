"""MCP tool: documents.rename — change a document's title or original_name."""
from __future__ import annotations

from typing import Any

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.mcp_tools._helpers import coerce_uuid

DESCRIPTOR = ToolDescriptor(
    name="documents.rename",
    description=(
        "Rename a document. Updates the user-facing 'title' field by "
        "default; pass field='original_name' to rewrite the displayed "
        "filename. The on-disk filename never changes."
    ),
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("write", "document")],
    input_schema={
        "type": "object",
        "required": ["document_id", "new_name"],
        "properties": {
            "document_id": {"type": "string"},
            "new_name": {"type": "string", "minLength": 1, "maxLength": 255},
            "field": {
                "type": "string",
                "enum": ["title", "original_name"],
                "default": "title",
            },
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "renamed": {"type": "object"},
            "error": {"type": "string"},
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    document_id: str,
    new_name: str,
    field: str = "title",
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import Document

    if field not in {"title", "original_name"}:
        return {"error": f"invalid field {field!r}; must be 'title' or 'original_name'"}
    if not new_name or not new_name.strip():
        return {"error": "new_name must not be empty"}

    did = coerce_uuid(document_id)
    if did is None:
        return {"error": "invalid document_id"}

    doc = db.get(Document, did)
    if doc is None:
        return {"error": "document not found"}

    before = getattr(doc, field)
    setattr(doc, field, new_name.strip())
    db.commit()

    return {
        "renamed": {
            "document_id": str(doc.id),
            "field": field,
            "before": before,
            "after": new_name.strip(),
        }
    }


register_tool(DESCRIPTOR, handler)
