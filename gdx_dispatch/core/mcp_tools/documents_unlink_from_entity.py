"""MCP tool: documents.unlink_from_entity — clear customer_id or job_id."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


DESCRIPTOR = ToolDescriptor(
    name="documents.unlink_from_entity",
    description=(
        "Clear a document's customer_id or job_id link. Reversible — "
        "use documents.link_to_entity to restore."
    ),
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("write", "document")],
    input_schema={
        "type": "object",
        "required": ["document_id", "entity_type"],
        "properties": {
            "document_id": {"type": "string"},
            "entity_type": {"type": "string", "enum": ["customer", "job"]},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "unlinked": {"type": "object"},
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
    entity_type: str,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import Document

    if entity_type not in {"customer", "job"}:
        return {"error": f"invalid entity_type {entity_type!r}"}

    did = _coerce_uuid(document_id)
    if did is None:
        return {"error": "invalid document_id"}

    doc = db.get(Document, did)
    if doc is None:
        return {"error": "document not found"}

    if entity_type == "customer":
        before = doc.customer_id
        doc.customer_id = None
        db.commit()
        return {
            "unlinked": {
                "document_id": str(doc.id),
                "entity_type": "customer",
                "before_customer_id": str(before) if before else None,
            }
        }

    before = doc.job_id
    doc.job_id = None
    db.commit()
    return {
        "unlinked": {
            "document_id": str(doc.id),
            "entity_type": "job",
            "before_job_id": str(before) if before else None,
        }
    }


register_tool(DESCRIPTOR, handler)
