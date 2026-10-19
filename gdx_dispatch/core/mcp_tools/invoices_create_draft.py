from __future__ import annotations

from typing import Any

from gdx_dispatch.core.mcp_registry import ToolDescriptor, register_tool

DESCRIPTOR = ToolDescriptor(
    name="invoices.create_draft",
    description=(
        "Not wired yet: refuses every call and creates nothing. Bill a "
        "completed job from its closeout or from Ready for Billing."
    ),
    blast_radius="yellow",
    approval_required=True,
    sensitivity_class="internal",
    capabilities_required=[("write", "invoice")],
    input_schema={
        "type": "object",
        "required": ["job_id"],
        "properties": {
            "job_id": {"type": "string"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "draft": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "customer_id": {"type": "string"},
                    "title": {"type": "string"},
                    "line_items": {"type": "array", "items": {"type": "object"}},
                    "total": {"type": "number"},
                },
            },
        },
    },
)


async def handler(
    principal: Any, db: Any, job_id: str, **_: Any
) -> dict[str, Any]:
    """Refuse: no invoice-drafting write backs this tool yet (GDXA-1849).

    It looked the job up through a local placeholder dataclass, which raises
    on a real Session, and on a mock it returned a ``draft`` with no lines
    and a zero total while inserting nothing. Drafting a bill applies
    billing's line, tax and claim rules; this tool gets them when
    money-billing exposes a shared function.
    """
    raise NotImplementedError(
        "invoices.create_draft is not wired to billing yet; no invoice was "
        "created. Bill the job from its closeout or Ready for Billing."
    )


register_tool(DESCRIPTOR, handler)
