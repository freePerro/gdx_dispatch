"""MCP tool: documents.move — move a document to a different folder. Green."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.mcp_tools._helpers import coerce_uuid

DESCRIPTOR = ToolDescriptor(
    name="documents.move",
    description=(
        "Move a document to a different folder. Green tool — applied directly. "
        "Pass folder_id=null to move to root."
    ),
    blast_radius="green",
    approval_required=False,
    sensitivity_class="internal",
    capabilities_required=[("write", "document")],
    input_schema={
        "type": "object",
        "required": ["document_id"],
        "properties": {
            "document_id": {"type": "string"},
            "folder_id": {
                "type": ["string", "null"],
                "description": "Target folder UUID; null = unfiled (root)",
            },
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "moved": {"type": "object"},
            "error": {"type": "string"},
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    document_id: str,
    folder_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import Document, DocumentFolder

    did = coerce_uuid(document_id)
    if did is None:
        return {"error": "invalid document_id"}

    fid: UUID | None = None
    if folder_id is not None:
        fid = coerce_uuid(folder_id)
        if fid is None:
            return {"error": "invalid folder_id"}

    doc = db.get(Document, did)
    if doc is None:
        return {"error": "document not found"}

    target_name: str | None = None
    if fid is not None:
        folder = db.get(DocumentFolder, fid)
        if folder is None or folder.deleted_at is not None:
            return {"error": "folder not found"}
        target_name = folder.name

    before_id = doc.folder_id
    before_name: str | None = None
    if before_id is not None:
        bf = db.get(DocumentFolder, before_id)
        before_name = bf.name if bf is not None else None

    doc.folder_id = fid
    db.commit()

    return {
        "moved": {
            "document_id": str(doc.id),
            "filename": doc.original_name,
            "before_folder_id": str(before_id) if before_id else None,
            "before_folder_name": before_name,
            "after_folder_id": str(fid) if fid else None,
            "after_folder_name": target_name,
        }
    }


register_tool(DESCRIPTOR, handler)
