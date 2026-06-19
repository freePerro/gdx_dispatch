"""MCP tool: documents.create_folder — create a new document folder."""
from __future__ import annotations

import uuid as _uuid
from typing import Any

from sqlalchemy import select

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


MAX_FOLDER_DEPTH = 15


DESCRIPTOR = ToolDescriptor(
    name="documents.create_folder",
    description=(
        "Create a new document folder. Returns the folder id. If a non-deleted "
        "folder with the same name AND parent already exists, returns that "
        "folder instead of creating a duplicate. Pass parent_id to nest under "
        "an existing folder; omit for a root-level folder."
    ),
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("write", "document.folder")],
    input_schema={
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "parent_id": {"type": "string", "description": "Optional parent folder UUID."},
            "description": {"type": "string", "maxLength": 2000},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "folder": {"type": "object"},
            "error": {"type": "string"},
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    name: str,
    parent_id: str | None = None,
    description: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import DocumentFolder

    cleaned = (name or "").strip()
    if not cleaned:
        return {"error": "name must not be empty"}
    if len(cleaned) > 200:
        return {"error": "name exceeds 200 chars"}

    parent_uuid = None
    if parent_id:
        try:
            parent_uuid = _uuid.UUID(parent_id)
        except (ValueError, AttributeError):
            return {"error": "parent_id is not a valid UUID"}
        parent = db.execute(
            select(DocumentFolder)
            .where(DocumentFolder.id == parent_uuid)
            .where(DocumentFolder.deleted_at.is_(None))
        ).scalar_one_or_none()
        if parent is None:
            return {"error": "parent folder not found"}
        # Walk up to enforce max depth.
        depth = 1
        cursor = parent
        seen: set[str] = set()
        while cursor.parent_id is not None:
            depth += 1
            if depth >= MAX_FOLDER_DEPTH:
                return {"error": f"folder nesting exceeds max depth of {MAX_FOLDER_DEPTH}"}
            cid = str(cursor.parent_id)
            if cid in seen:
                return {"error": "folder hierarchy contains a cycle"}
            seen.add(cid)
            cursor = db.execute(
                select(DocumentFolder).where(DocumentFolder.id == cursor.parent_id)
            ).scalar_one_or_none()
            if cursor is None:
                break

    existing = db.execute(
        select(DocumentFolder)
        .where(DocumentFolder.name == cleaned)
        .where(DocumentFolder.parent_id == parent_uuid if parent_uuid else DocumentFolder.parent_id.is_(None))
        .where(DocumentFolder.deleted_at.is_(None))
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return {
            "folder": {
                "id": str(existing.id),
                "name": existing.name,
                "parent_id": str(existing.parent_id) if existing.parent_id else None,
                "description": existing.description,
                "reused": True,
            }
        }

    created_by = None
    for attr in ("user_id", "id", "sub"):
        v = getattr(principal, attr, None)
        if v:
            created_by = str(v)
            break

    folder = DocumentFolder(
        name=cleaned,
        parent_id=parent_uuid,
        description=description,
        created_by=created_by,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)

    return {
        "folder": {
            "id": str(folder.id),
            "name": folder.name,
            "parent_id": str(folder.parent_id) if folder.parent_id else None,
            "description": folder.description,
            "reused": False,
        }
    }


register_tool(DESCRIPTOR, handler)
