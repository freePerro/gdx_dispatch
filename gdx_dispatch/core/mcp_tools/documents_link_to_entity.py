"""MCP tool: documents.link_to_entity — attach a document to a customer or job."""
from __future__ import annotations

from typing import Any

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.mcp_tools._helpers import coerce_uuid

DESCRIPTOR = ToolDescriptor(
    name="documents.link_to_entity",
    description=(
        "Attach a document to a customer or job by setting the FK column. "
        "entity_type='customer' sets customer_id; entity_type='job' sets job_id. "
        "Both can be linked simultaneously by calling twice."
    ),
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("write", "document")],
    input_schema={
        "type": "object",
        "required": ["document_id", "entity_type", "entity_id"],
        "properties": {
            "document_id": {"type": "string"},
            "entity_type": {"type": "string", "enum": ["customer", "job"]},
            "entity_id": {"type": "string"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "linked": {"type": "object"},
            "error": {"type": "string"},
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    document_id: str,
    entity_type: str,
    entity_id: str,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import Customer, Document, Job

    if entity_type not in {"customer", "job"}:
        return {"error": f"invalid entity_type {entity_type!r}"}

    did = coerce_uuid(document_id)
    if did is None:
        return {"error": "invalid document_id"}
    eid = coerce_uuid(entity_id)
    if eid is None:
        return {"error": "invalid entity_id"}

    doc = db.get(Document, did)
    if doc is None:
        return {"error": "document not found"}

    if entity_type == "customer":
        target = db.get(Customer, eid)
        if target is None:
            return {"error": "customer not found"}
        before = doc.customer_id
        doc.customer_id = eid
        db.commit()
        return {
            "linked": {
                "document_id": str(doc.id),
                "entity_type": "customer",
                "before_customer_id": str(before) if before else None,
                "after_customer_id": str(eid),
            }
        }

    target = db.get(Job, eid)
    if target is None:
        return {"error": "job not found"}
    before = doc.job_id
    doc.job_id = eid
    db.commit()
    return {
        "linked": {
            "document_id": str(doc.id),
            "entity_type": "job",
            "before_job_id": str(before) if before else None,
            "after_job_id": str(eid),
        }
    }


register_tool(DESCRIPTOR, handler)
