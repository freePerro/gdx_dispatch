"""MCP tool: documents.set_tags — overwrite a document's tags."""
from __future__ import annotations

from typing import Any

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.mcp_tools._helpers import coerce_uuid

DESCRIPTOR = ToolDescriptor(
    name="documents.set_tags",
    description=(
        "Replace the tag list on a document. Tags are stored as a "
        "comma-separated string. Pass an empty list to clear."
    ),
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("write", "document")],
    input_schema={
        "type": "object",
        "required": ["document_id", "tags"],
        "properties": {
            "document_id": {"type": "string"},
            "tags": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 60},
                "maxItems": 25,
            },
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "tagged": {"type": "object"},
            "error": {"type": "string"},
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    document_id: str,
    tags: list[str],
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import Document

    did = coerce_uuid(document_id)
    if did is None:
        return {"error": "invalid document_id"}

    if tags is None:
        return {"error": "tags must be a list (use [] to clear)"}

    cleaned = [t.strip() for t in tags if t and t.strip()]
    seen: set[str] = set()
    deduped: list[str] = []
    for t in cleaned:
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(t)
    new_value = ",".join(deduped) if deduped else None
    if new_value and len(new_value) > 500:
        return {"error": "combined tag string exceeds 500 chars"}

    doc = db.get(Document, did)
    if doc is None:
        return {"error": "document not found"}

    before = doc.tags
    doc.tags = new_value
    db.commit()

    return {
        "tagged": {
            "document_id": str(doc.id),
            "before": before,
            "after": new_value,
            "count": len(deduped),
        }
    }


register_tool(DESCRIPTOR, handler)
