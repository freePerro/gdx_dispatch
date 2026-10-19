from __future__ import annotations

from typing import Any

from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool
from gdx_dispatch.core.mcp_tools._helpers import coerce_uuid
from gdx_dispatch.models.tenant_models import Job

DESCRIPTOR = ToolDescriptor(
    name="jobs.detail",
    description="Look up a single job by id.",
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "job")],
    input_schema={
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
        },
        "required": ["job_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "job": {
                "type": ["object", "null"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "lifecycle_stage": {"type": "string"},
                    "customer_id": {"type": "string"},
                    "scheduled_at": {"type": ["string", "null"]},
                    "completed_at": {"type": ["string", "null"]},
                },
            },
        },
    },
)


async def handler(principal: Any, db: Any, job_id: str, **_: Any) -> dict[str, Any]:
    """Look up a single job by id."""
    # A soft-deleted job is not found (invariant #2), and the id is bound as a
    # UUID: SQLite stores the column as dashless hex.
    jid = coerce_uuid(job_id)
    row = db.get(Job, jid) if jid is not None else None
    if row is None or row.deleted_at is not None:
        return {"job": None}

    return {
        "job": {
            "id": str(row.id),
            "title": row.title,
            "lifecycle_stage": row.lifecycle_stage,
            "customer_id": str(row.customer_id),
            "scheduled_at": row.scheduled_at,
            "completed_at": row.completed_at,
        }
    }


register_tool(DESCRIPTOR, handler)
