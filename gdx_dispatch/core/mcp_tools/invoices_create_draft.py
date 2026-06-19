from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool


@dataclass
class Job:
    """Mock Job class for type hinting/db.get usage."""
    id: str
    title: str
    customer_id: str
    lifecycle_stage: str
    deleted_at: Any = None


DESCRIPTOR = ToolDescriptor(
    name="invoices.create_draft",
    description="Draft invoice from a completed job. Yellow.",
    blast_radius="yellow",
    approval_required=True,
    sensitivity_class="internal",
    capabilities_required=[("write", "invoice")],
    input_schema={
        "type": "object",
        "required": ["job_id"],
        "properties": {
            "job_id": {"type": "string"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "draft": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "customer_id": {"type": "string"},
                    "title": {"type": "string"},
                    "line_items": {"type": "array", "items": {"type": "object"}},
                    "total": {"type": "number"},
                },
            },
        },
    },
)


async def handler(
    principal: Any, db: Any, job_id: str, **_
) -> dict[str, Any]:
    """
    Handler for creating an invoice draft.
    Note: invoke_tool handles the approval_ref gate and capability checks.
    """
    # In a real app, Job would be imported from the domain model.
    # Here we use the local definition or assume db.get returns an object with these attrs.
    job = db.get(Job, job_id)
    if job is None:
        return {"error": "job not found"}

    # Build a draft preview.
    # For v1, we return the preview shape. Actual DB insert logic is deferred.
    draft = {
        "job_id": str(job.id),
        "customer_id": str(job.customer_id),
        "title": f"Draft for {job.title}",
        "line_items": [],
        "total": 0.0,
    }

    return {"draft": draft}


register_tool(DESCRIPTOR, handler)
