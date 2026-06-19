from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool
from gdx_dispatch.models.tenant_models import Job


DESCRIPTOR = ToolDescriptor(
    name="jobs.list",
    description="List jobs with optional filters (status/customer_id/technician_id/since).",
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "job")],
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["open", "scheduled", "completed", "all"],
            },
            "customer_id": {"type": "string"},
            "technician_id": {"type": "string"},
            "since": {"type": "string", "format": "date-time"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "jobs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "status": {"type": "string"},
                        "customer_id": {"type": "string"},
                        "scheduled_at": {"type": ["string", "null"]},
                        "completed_at": {"type": ["string", "null"]},
                    },
                },
            },
            "truncated": {"type": "boolean"},
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    status: str | None = None,
    customer_id: str | None = None,
    technician_id: str | None = None,
    since: str | None = None,
    **_,
) -> dict[str, Any]:
    """List jobs with optional filters."""
    stmt = select(Job)

    if status == "open":
        stmt = stmt.where(Job.lifecycle_stage.in_(("lead", "estimate", "scheduled", "in_progress")))
    elif status == "scheduled":
        stmt = stmt.where(Job.lifecycle_stage == "scheduled")
    elif status == "completed":
        stmt = stmt.where(Job.lifecycle_stage == "completed")
    # "all" implies no status filter

    if customer_id:
        stmt = stmt.where(Job.customer_id == customer_id)

    if technician_id:
        stmt = stmt.where(Job.assigned_to == technician_id)

    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            stmt = stmt.where(Job.created_at >= since_dt)
        except ValueError:
            return {"ok": False, "error_type": "validation_error", "error_body": "Invalid ISO date format for 'since'"}

    # Limit to 50
    stmt = stmt.limit(51)
    result = db.execute(stmt)
    rows = result.scalars().all()

    truncated = False
    if len(rows) > 50:
        rows = rows[:50]
        truncated = True

    jobs_payload = []
    for job in rows:
        jobs_payload.append({
            "id": str(job.id),
            "title": job.title,
            "status": job.status,
            "customer_id": str(job.customer_id),
            "scheduled_at": job.scheduled_at.isoformat() if job.scheduled_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        })

    return {
        "jobs": jobs_payload,
        "truncated": truncated,
    }


register_tool(DESCRIPTOR, handler)
