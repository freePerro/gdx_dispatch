"""MCP tool: documents.search — fuzzy search across filename/title/tags."""
from __future__ import annotations

from typing import Any

from sqlalchemy import desc, or_, select

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


DESCRIPTOR = ToolDescriptor(
    name="documents.search",
    description=(
        "Search documents by query string (matches filename, original_name, "
        "title, description, tags). Optionally filter by content_type and "
        "uploaded date range."
    ),
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "document")],
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Substring matched against filename/title/tags"},
            "content_type": {"type": "string", "description": "Substring match on content_type"},
            "since": {"type": "string", "description": "ISO date — uploaded_at >= this"},
            "until": {"type": "string", "description": "ISO date — uploaded_at <= this"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
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


async def handler(
    principal: Any,
    db: Any,
    query: str | None = None,
    content_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 25,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import Document

    capped = max(1, min(int(limit or 25), 100))

    stmt = select(Document).where(Document.deleted_at.is_(None))
    if query:
        like = f"%{query}%"
        stmt = stmt.where(
            or_(
                Document.filename.ilike(like),
                Document.original_name.ilike(like),
                Document.title.ilike(like),
                Document.description.ilike(like),
                Document.tags.ilike(like),
            )
        )
    if content_type:
        stmt = stmt.where(Document.content_type.ilike(f"%{content_type}%"))
    if since:
        stmt = stmt.where(Document.uploaded_at >= since)
    if until:
        stmt = stmt.where(Document.uploaded_at <= until)

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
                "content_type": d.content_type,
                "file_size": int(d.file_size or 0),
                "folder_id": str(d.folder_id) if d.folder_id else None,
                "tags": d.tags,
                "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None,
            }
            for d in rows
        ],
        "truncated": truncated,
    }


register_tool(DESCRIPTOR, handler)
