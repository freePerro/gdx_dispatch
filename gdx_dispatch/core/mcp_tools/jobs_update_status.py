from __future__ import annotations

from typing import Any

from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool
from gdx_dispatch.core.mcp_tools._helpers import live_job
from gdx_dispatch.models.tenant_models import Job

# Preview only, and so green (GDXA-1881). A stage change belongs to the job
# page's own verbs — PATCH through the visit planner, Close out, Cancel job,
# Re-open — each with its own guard and audit row, and no service the
# assistant could call. The tool used to be yellow and never wrote, so its
# Apply button answered "Applied" while the job stayed where it was.
LIFECYCLE_STAGES = tuple(Job.__table__.c.lifecycle_stage.type.enums)

DESCRIPTOR = ToolDescriptor(
    name="jobs.update_status",
    description=(
        "Preview a change to a job's lifecycle_stage. Read-only: it never "
        "changes the job (applied is always false). allowed=false with a "
        "detail means the job page refuses that change as a plain stage "
        "change (finish with Close out, cancel with Cancel job, change a "
        "finished job with Re-open). A person makes the change on the job page."
    ),
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "job")],
    input_schema={
        "type": "object",
        "required": ["job_id", "new_status"],
        "properties": {
            "job_id": {"type": "string"},
            "new_status": {"type": "string", "enum": list(LIFECYCLE_STAGES)},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "applied": {"type": "boolean"},
            "preview": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "before": {"type": "string"},
                    "after": {"type": "string"},
                },
            },
            "allowed": {"type": "boolean"},
            "detail": {"type": "string"},
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    job_id: str,
    new_status: str,
    **_: Any,
) -> dict[str, Any]:
    """Say what the stage change would be and whether it is allowed."""
    # Imported here: routers.jobs imports far more than a tool module should
    # pull in at registration time.
    from gdx_dispatch.routers.jobs import _stage_change_refusal

    # Raised, not returned: invoke_tool reports a returned error dict as a
    # successful result.
    if new_status not in LIFECYCLE_STAGES:
        raise ValueError(f"new_status is not a lifecycle stage: {new_status!r}")
    job = live_job(db, job_id)

    preview = {"job_id": str(job.id), "before": job.lifecycle_stage, "after": new_status}
    if job.lifecycle_stage == new_status:
        return {"applied": False, "preview": preview, "allowed": False,
                "detail": f"Not changed. The job is already {new_status}."}
    refusal = _stage_change_refusal(job.lifecycle_stage, new_status)
    if refusal is not None:
        return {"applied": False, "preview": preview, "allowed": False,
                "detail": "Not changed. " + refusal["detail"]}
    return {
        "applied": False,
        "preview": preview,
        "allowed": True,
        "detail": "Not changed. Change the stage on the job page.",
    }


register_tool(DESCRIPTOR, handler)
