from __future__ import annotations

from typing import Any
from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool

# Note: Job model is used in the handler, but we don't import it directly
# to avoid circular dependencies if it's not needed for the descriptor.
# The handler receives 'db' which is used to fetch the Job.

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
    # The implementation notes say sync db.get. No await.
    # We assume 'Job' is available in the scope or passed via db context,
    # but the test uses _mock_db which just calls db.get(row).
    # In a real scenario, we'd use db.get(Job, job_id).
    # Looking at the test: db.get.return_value = row.
    # The test doesn't specify the class passed to db.get.
    # However, the requirement says: row = db.get(Job, job_id)
    # Since I don't have the Job class definition in ground truth,
    # and I cannot import it without knowing the path (though research says gdx_dispatch/models/tenant_models.py:Job),
    # I will use a placeholder or assume Job is available if I were to import it.
    # But wait, the requirement explicitly says: `row = db.get(Job, job_id)`.
    # I'll try to import it.

    from gdx_dispatch.models.tenant_models import Job

    row = db.get(Job, job_id)

    if row is None:
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
