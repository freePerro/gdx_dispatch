from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool
from gdx_dispatch.models.tenant_models import Job

DESCRIPTOR = ToolDescriptor(
    name="schedule.lookup",
    description="List jobs scheduled in a date window, ordered by scheduled_at. Cancelled jobs are excluded.",
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "schedule"), ("read", "job")],
    input_schema={
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date-time"},
            "end": {"type": "string", "format": "date-time"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "schedule": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "title": {"type": "string"},
                        "scheduled_at": {"type": ["string", "null"]},
                        "customer_id": {"type": "string"},
                        "technician_id": {"type": "string"},
                        "lifecycle_stage": {"type": "string"},
                    },
                },
            },
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    start: str | None = None,
    end: str | None = None,
    **_,
) -> dict[str, Any]:
    """List jobs scheduled in a date window."""
    now = datetime.now(timezone.utc)

    if start:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    else:
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)

    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else start_dt + timedelta(days=7)

    # A cancelled job keeps its scheduled_at (cancel sets lifecycle_stage
    # only), so without this filter the assistant reported cancelled visits as
    # booked work. Completed jobs stay: the window can lie in the past, and
    # "what was on the schedule last Tuesday" includes the work that got done.
    # lifecycle_stage is in the output so the caller can tell them apart.
    stmt = (
        select(Job)
        .where(
            Job.scheduled_at >= start_dt,
            Job.scheduled_at < end_dt,
            Job.deleted_at.is_(None),
            Job.lifecycle_stage != "cancelled",
        )
        .order_by(Job.scheduled_at)
    )
    rows = db.execute(stmt).scalars().all()

    schedule = []
    for r in rows:
        schedule.append({
            "job_id": str(r.id),
            "title": r.title,
            "scheduled_at": r.scheduled_at.isoformat() if r.scheduled_at else None,
            "customer_id": str(r.customer_id),
            "technician_id": r.assigned_to,
            "lifecycle_stage": r.lifecycle_stage,
        })

    return {"schedule": schedule}


register_tool(DESCRIPTOR, handler)
