"""MCP tool: documents.list — list documents, optionally filtered by folder."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import desc, select

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


DESCRIPTOR = ToolDescriptor(
    name="documents.list",
    description="List documents, optionally filtered by folder, customer, or job.",
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "document")],
    input_schema={
        "type": "object",
        "properties": {
            "folder_id": {"type": "string"},
            "customer_id": {"type": "string"},
            "job_id": {"type": "string"},
            "include_deleted": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "documents": {"type": "array", "items": {"type": "object"}},
            "truncated": {"type": "boolean"},
        },
    },
)


def _coerce_uuid(raw: str | None) -> UUID | None:
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


async def handler(
    principal: Any,
    db: Any,
    folder_id: str | None = None,
    customer_id: str | None = None,
    job_id: str | None = None,
    include_deleted: bool = False,
    limit: int = 50,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import Document

    capped = max(1, min(int(limit or 50), 200))
    stmt = select(Document)

    f_uuid = _coerce_uuid(folder_id)
    if folder_id and f_uuid is None:
        return {"documents": [], "truncated": False, "error": "invalid folder_id"}
    if f_uuid is not None:
        stmt = stmt.where(Document.folder_id == f_uuid)

    c_uuid = _coerce_uuid(customer_id)
    if customer_id and c_uuid is None:
        return {"documents": [], "truncated": False, "error": "invalid customer_id"}
    if c_uuid is not None:
        stmt = stmt.where(Document.customer_id == c_uuid)

    j_uuid = _coerce_uuid(job_id)
    if job_id and j_uuid is None:
        return {"documents": [], "truncated": False, "error": "invalid job_id"}
    if j_uuid is not None:
        stmt = stmt.where(Document.job_id == j_uuid)

    if not include_deleted:
        stmt = stmt.where(Document.deleted_at.is_(None))

    stmt = stmt.order_by(desc(Document.uploaded_at)).limit(capped + 1)
    rows = list(db.execute(stmt).scalars().all())
    truncated = len(rows) > capped
    rows = rows[:capped]

    return {
        "documents": [
            {
                "id": str(d.id),
                "filename": d.filename,
                "original_name": d.original_name,
                "title": d.title,
                "description": d.description,
                "content_type": d.content_type,
                "file_size": int(d.file_size or 0),
                "folder_id": str(d.folder_id) if d.folder_id else None,
                "customer_id": str(d.customer_id) if d.customer_id else None,
                "job_id": str(d.job_id) if d.job_id else None,
                "tags": d.tags,
                "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None,
                "deleted_at": d.deleted_at.isoformat() if d.deleted_at else None,
            }
            for d in rows
        ],
        "truncated": truncated,
    }


register_tool(DESCRIPTOR, handler)
