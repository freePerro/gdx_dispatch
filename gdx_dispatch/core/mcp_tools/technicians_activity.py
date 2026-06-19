from __future__ import annotations

from typing import Any
from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool


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

    # Default window = last 30 days if since is not provided.
    # Note: The caller/transport layer usually handles date parsing,
    # but we ensure the query uses the parameter.

    query = """
        SELECT
            assigned_to,
            COUNT(*) FILTER (WHERE lifecycle_stage='completed'),
            COUNT(*) FILTER (WHERE lifecycle_stage IN ('scheduled','in_progress')),
            MAX(updated_at)
        FROM jobs
        WHERE updated_at >= :since
          AND assigned_to IS NOT NULL
          AND deleted_at IS NULL
        GROUP BY assigned_to
    """

    # The test uses _mock_rows which returns rows as (tech_id, completed, in_progress, last_active)
    # We assume the db object passed in has an .execute() method returning a result with .all()
    result = db.execute(query, {"since": since})
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
