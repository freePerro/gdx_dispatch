"""MCP tool: documents.read — metadata + download URL for one document."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


DESCRIPTOR = ToolDescriptor(
    name="documents.read",
    description=(
        "Fetch one document's metadata and the API download URL. The AI "
        "uses documents.summarize for content; documents.read returns the "
        "URL the user can click to retrieve the file."
    ),
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "document")],
    input_schema={
        "type": "object",
        "required": ["document_id"],
        "properties": {
            "document_id": {"type": "string"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "document": {"type": "object"},
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
    document_id: str,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import Document

    did = _coerce_uuid(document_id)
    if did is None:
        return {"error": "invalid document_id"}

    doc = db.get(Document, did)
    if doc is None:
        return {"error": "document not found"}

    return {
        "document": {
            "id": str(doc.id),
            "filename": doc.filename,
            "original_name": doc.original_name,
            "title": doc.title,
            "description": doc.description,
            "content_type": doc.content_type,
            "file_size": int(doc.file_size or 0),
            "folder_id": str(doc.folder_id) if doc.folder_id else None,
            "customer_id": str(doc.customer_id) if doc.customer_id else None,
            "job_id": str(doc.job_id) if doc.job_id else None,
            "tags": doc.tags,
            "uploaded_at": doc.uploaded_at.isoformat() if doc.uploaded_at else None,
            "deleted_at": doc.deleted_at.isoformat() if doc.deleted_at else None,
            "download_url": f"/api/documents/{doc.id}/download",
        }
    }


register_tool(DESCRIPTOR, handler)
