"""MCP tool: documents.bulk_move — move many documents to one folder. Yellow."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


MAX_IDS = 500


DESCRIPTOR = ToolDescriptor(
    name="documents.bulk_move",
    description=(
        "Move multiple documents to a single target folder. Yellow tool — "
        "first call returns a preview (count + sample), second call with "
        "approval_ref applies. Pass folder_id=null to move to root. "
        f"Cap: {MAX_IDS} ids per call."
    ),
    blast_radius="yellow",
    approval_required=True,
    sensitivity_class="internal",
    capabilities_required=[("write", "document")],
    input_schema={
        "type": "object",
        "required": ["document_ids"],
        "properties": {
            "document_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": MAX_IDS,
            },
            "folder_id": {
                "type": ["string", "null"],
                "description": "Target folder UUID; null = unfiled (root)",
            },
            "approval_ref": {
                "type": "string",
                "description": "Echo of the approval token returned in the 202 preview response",
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


def _coerce_uuid(raw: str | None) -> UUID | None:
    if raw is None:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


async def handler(
    principal: Any,
    db: Any,
    document_ids: list[str],
    folder_id: str | None = None,
    approval_ref: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    from sqlalchemy import select

    from gdx_dispatch.models.tenant_models import Document, DocumentFolder

    if not isinstance(document_ids, list) or not document_ids:
        return {"error": "document_ids must be a non-empty list"}
    if len(document_ids) > MAX_IDS:
        return {"error": f"too many ids: {len(document_ids)} > {MAX_IDS}"}

    dids: list[UUID] = []
    invalid: list[str] = []
    for raw in document_ids:
        u = _coerce_uuid(raw)
        if u is None:
            invalid.append(str(raw))
        else:
            dids.append(u)
    if invalid:
        return {"error": f"invalid document_ids: {invalid[:5]}"}

    fid: UUID | None = None
    target_name: str | None = None
    if folder_id is not None:
        fid = _coerce_uuid(folder_id)
        if fid is None:
            return {"error": "invalid folder_id"}
        folder = db.get(DocumentFolder, fid)
        if folder is None or folder.deleted_at is not None:
            return {"error": "folder not found"}
        target_name = folder.name

    docs = list(db.execute(select(Document).where(Document.id.in_(dids))).scalars())
    found_ids = {d.id for d in docs}
    missing = [str(d) for d in dids if d not in found_ids]

    if not approval_ref:
        sample = [
            {
                "document_id": str(d.id),
                "filename": d.original_name,
                "before_folder_id": str(d.folder_id) if d.folder_id else None,
            }
            for d in docs[:10]
        ]
        return {
            "moved": {
                "preview": True,
                "requested": len(dids),
                "found": len(docs),
                "missing": missing,
                "after_folder_id": str(fid) if fid else None,
                "after_folder_name": target_name,
                "sample": sample,
            }
        }

    moved_ids: list[str] = []
    for doc in docs:
        doc.folder_id = fid
        moved_ids.append(str(doc.id))
    db.commit()

    return {
        "moved": {
            "preview": False,
            "requested": len(dids),
            "moved_count": len(moved_ids),
            "moved_ids": moved_ids,
            "missing": missing,
            "after_folder_id": str(fid) if fid else None,
            "after_folder_name": target_name,
        }
    }


register_tool(DESCRIPTOR, handler)
