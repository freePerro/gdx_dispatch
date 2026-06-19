"""MCP tool: documents.rename_folder — rename a document folder."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


DESCRIPTOR = ToolDescriptor(
    name="documents.rename_folder",
    description="Rename an existing document folder.",
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("write", "document.folder")],
    input_schema={
        "type": "object",
        "required": ["folder_id", "new_name"],
        "properties": {
            "folder_id": {"type": "string"},
            "new_name": {"type": "string", "minLength": 1, "maxLength": 200},
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


def _coerce_uuid(raw: str) -> UUID | None:
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


async def handler(
    principal: Any,
    db: Any,
    folder_id: str,
    new_name: str,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import DocumentFolder

    fid = _coerce_uuid(folder_id)
    if fid is None:
        return {"error": "invalid folder_id"}

    cleaned = (new_name or "").strip()
    if not cleaned:
        return {"error": "new_name must not be empty"}
    if len(cleaned) > 200:
        return {"error": "new_name exceeds 200 chars"}

    folder = db.get(DocumentFolder, fid)
    if folder is None or folder.deleted_at is not None:
        return {"error": "folder not found"}

    before = folder.name
    folder.name = cleaned
    db.commit()

    return {
        "renamed": {
            "folder_id": str(folder.id),
            "before": before,
            "after": cleaned,
        }
    }


register_tool(DESCRIPTOR, handler)
