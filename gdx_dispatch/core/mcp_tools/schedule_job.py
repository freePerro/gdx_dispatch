from __future__ import annotations

from typing import Any

from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool

DESCRIPTOR = ToolDescriptor(
    name="schedule.schedule_job",
    description=(
        "Not wired yet: refuses every call and changes nothing. Schedule the "
        "job from the dispatch board."
    ),
    blast_radius="yellow",
    approval_required=True,
    sensitivity_class="internal",
    capabilities_required=[("write", "schedule")],
    input_schema={
        "type": "object",
        "required": ["job_id", "technician_id", "scheduled_at"],
        "properties": {
            "job_id": {"type": "string"},
            "technician_id": {"type": "string"},
            "scheduled_at": {"type": "string"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "scheduled": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "before": {
                        "type": "object",
                        "properties": {
                            "technician_id": {"type": "string", "nullable": True},
                            "scheduled_at": {"type": "string", "nullable": True},
                        },
                    },
                    "after": {
                        "type": "object",
                        "properties": {
                            "technician_id": {"type": "string"},
                            "scheduled_at": {"type": "string"},
                        },
                    },
                },
            }
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    job_id: str,
    technician_id: str,
    scheduled_at: str,
    **_: Any,
) -> dict[str, Any]:
    """Refuse: no scheduling write backs this tool yet (GDXA-1849).

    It looked the job up through a local placeholder class, which raises
    ``NoInspectionAvailable`` on a real Session, and on a mock it returned
    ``ok=True`` with a ``scheduled`` before/after while assigning nothing.
    Scheduling has side effects the dispatch side owns; this tool gets them
    when jobs-dispatch exposes a shared function.
    """
    raise NotImplementedError(
        "schedule.schedule_job is not wired to scheduling yet; nothing was "
        "changed. Schedule the job from the dispatch board."
    )


register_tool(DESCRIPTOR, handler)
