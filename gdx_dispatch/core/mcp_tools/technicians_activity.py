from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool
from gdx_dispatch.models.tenant_models import Job

DESCRIPTOR = ToolDescriptor(
    name="technicians.activity",
    description="Per-technician completed/in-progress job counts and last-active date over a window (default 30 days).",
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "technician"), ("read", "job")],
    input_schema={
        "type": "object",
        "properties": {
            "since": {"type": "string", "format": "date-time"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "technicians": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "technician_id": {"type": "string"},
                        "jobs_completed": {"type": "integer"},
                        "jobs_in_progress": {"type": "integer"},
                        "last_active": {"type": ["string", "null"]},
                    },
                    "required": ["technician_id", "jobs_completed", "jobs_in_progress", "last_active"],
                },
            },
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    since: str | None = None,
    **_ : Any
) -> dict[str, Any]:
    """
    Returns per-technician activity rollup.
    """
    # Default window = last 30 days, as the descriptor promises. The old SQL
    # string bound `since` as given, so an omitted `since` compared against
    # NULL and matched nothing — and Session.execute refuses a bare string
    # anyway, so the string form only ever ran against the unit test's mock
    # (GDXA-209).
    if since:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    else:
        since_dt = datetime.now(timezone.utc) - timedelta(days=30)

    stmt = (
        select(
            Job.assigned_to,
            func.count().filter(Job.lifecycle_stage == "completed"),
            func.count().filter(Job.lifecycle_stage.in_(("scheduled", "in_progress"))),
            func.max(Job.updated_at),
        )
        .where(
            Job.updated_at >= since_dt,
            Job.assigned_to.is_not(None),
            Job.deleted_at.is_(None),
        )
        .group_by(Job.assigned_to)
    )
    result = db.execute(stmt)
    rows = result.all()

    technicians = []
    for row in rows:
        # row is (assigned_to, completed_count, in_progress_count, last_active)
        technicians.append({
            "technician_id": str(row[0]),
            "jobs_completed": int(row[1]),
            "jobs_in_progress": int(row[2]),
            "last_active": str(row[3]) if row[3] is not None else None,
        })

    return {"technicians": technicians}


register_tool(DESCRIPTOR, handler)
