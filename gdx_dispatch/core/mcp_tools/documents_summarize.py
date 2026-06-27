"""MCP tool: documents.summarize — extract text from a document for the AI."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.mcp_tools._helpers import coerce_uuid

DESCRIPTOR = ToolDescriptor(
    name="documents.summarize",
    description=(
        "Extract text from a stored document (PDF, DOCX, TXT, MD, CSV) and "
        "return it. The AI assistant uses this output as raw material to "
        "generate a summary in its own response. Output is capped at 25,000 "
        "characters; longer files are truncated."
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
            "extracted": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "filename": {"type": "string"},
                    "content_type": {"type": "string"},
                    "text": {"type": "string"},
                    "truncated": {"type": "boolean"},
                    "char_count": {"type": "integer"},
                },
            },
            "error": {"type": "string"},
        },
    },
)


def _upload_dir() -> Path:
    return Path(os.getenv("UPLOAD_DIR", "/app/uploads/"))


async def handler(
    principal: Any,
    db: Any,
    document_id: str,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.core.document_text import extract_text
    from gdx_dispatch.models.tenant_models import Document

    did = coerce_uuid(document_id)
    if did is None:
        return {"error": "invalid document_id"}

    doc = db.get(Document, did)
    if doc is None:
        return {"error": "document not found"}
    if doc.deleted_at is not None:
        return {"error": "document is deleted"}

    path = _upload_dir() / doc.filename
    text, truncated, err = extract_text(path, doc.content_type)
    if err and not text:
        return {
            "extracted": {
                "document_id": str(doc.id),
                "filename": doc.original_name,
                "content_type": doc.content_type,
                "text": "",
                "truncated": False,
                "char_count": 0,
            },
            "error": err,
        }

    return {
        "extracted": {
            "document_id": str(doc.id),
            "filename": doc.original_name,
            "content_type": doc.content_type,
            "text": text,
            "truncated": truncated,
            "char_count": len(text),
        }
    }


register_tool(DESCRIPTOR, handler)
