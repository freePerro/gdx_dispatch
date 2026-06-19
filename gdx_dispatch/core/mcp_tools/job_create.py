"""MCP tool: job.create — create a job for a customer."""
from __future__ import annotations

from typing import Any

from gdx_dispatch.core.mcp_registry import register_tool, require_capability
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor

DESCRIPTOR = ToolDescriptor(
    name="job.create",
    description="Create a job for a given customer. Returns the created job's id + status.",
    input_schema={
        "type": "object",
        "required": ["customer_id", "description"],
        "properties": {
            "customer_id": {"type": "string"},
            "description": {"type": "string"},
            "scheduled_at": {"type": "string", "format": "date-time"},
            "address": {"type": "string"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "status": {"type": "string"},
        },
    },
    capabilities_required=[("write", "job")],
    sensitivity_class="internal",
)


async def handler(
    *,
    customer_id: str,
    description: str,
    scheduled_at: str | None = None,
    address: str | None = None,
    principal: Any = None,
    db: Any = None,
    **_ignored: Any,
) -> dict[str, Any]:
    """Create a job. write-scope so must pass capability gate before side-effects."""
    require_capability(principal, DESCRIPTOR)
    if not customer_id:
        raise ValueError("customer_id is required")
    if not description:
        raise ValueError("description is required")

    if db is None:
        # Stub surface until SS-19 wires transport + DB.
        return {"id": "stub-job-id", "status": "scheduled", "_stub": True}

    # Real wiring happens in SS-19; keep the surface honest here.
    from gdx_dispatch.models.platform import Job  # local import

    job = Job(
        customer_id=customer_id,
        description=description,
        scheduled_at=scheduled_at,
        address=address,
        status="scheduled",
    )
    db.add(job)
    db.flush()
    return {"id": str(job.id), "status": job.status}


register_tool(DESCRIPTOR, handler)
