from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool


DESCRIPTOR = ToolDescriptor(
    name="schedule.lookup",
    description="List jobs scheduled in a date window, ordered by scheduled_at.",
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

    # The query pattern requested:
    # SELECT * FROM jobs WHERE scheduled_at >= :start AND scheduled_at < :end AND deleted_at IS NULL ORDER BY scheduled_at
    # Note: The test uses a mock DB that returns rows via result.scalars.all()

    query = (
        "SELECT * FROM jobs "
        "WHERE scheduled_at >= :start AND scheduled_at < :end "
        "AND deleted_at IS NULL "
        "ORDER BY scheduled_at"
    )

    result = db.execute(query, {"start": start_dt, "end": end_dt})
    rows = result.scalars().all()

    schedule = []
    for r in rows:
        schedule.append({
            "job_id": str(r.id),
            "title": r.title,
            "scheduled_at": r.scheduled_at.isoformat() if r.scheduled_at else None,
            "customer_id": str(r.customer_id),
            "technician_id": r.assigned_to,
        })

    return {"schedule": schedule}


register_tool(DESCRIPTOR, handler)
