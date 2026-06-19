"""MCP tool: estimates.get — get a single estimate with its lines. Green."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


DESCRIPTOR = ToolDescriptor(
    name="estimates.get",
    description="Get a single estimate including all lines.",
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "estimate")],
    input_schema={
        "type": "object",
        "required": ["estimate_id"],
        "properties": {
            "estimate_id": {"type": "string"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "estimate": {"type": "object"},
            "error": {"type": "string"},
        },
    },
)


def _coerce_uuid(raw: str | None) -> UUID | None:
    if raw is None:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


async def handler(
    principal: Any,
    db: Any,
    estimate_id: str,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.modules.proposals.models import Estimate

    eid = _coerce_uuid(estimate_id)
    if eid is None:
        return {"error": "invalid estimate_id"}

    estimate = db.get(Estimate, eid)
    if estimate is None or estimate.deleted_at is not None:
        return {"error": "estimate not found"}

    lines = sorted(estimate.lines, key=lambda l: (l.sort_order or 0, l.created_at or 0))

    return {
        "estimate": {
            "id": str(estimate.id),
            "estimate_number": estimate.estimate_number,
            "customer_id": str(estimate.customer_id) if estimate.customer_id else None,
            "job_id": str(estimate.job_id) if estimate.job_id else None,
            "label": estimate.label,
            "description": getattr(estimate, "description", None),
            "notes": estimate.notes,
            "jobsite_address": estimate.jobsite_address,
            "status": estimate.status,
            "total": float(estimate.total or 0),
            "created_at": estimate.created_at.isoformat() if estimate.created_at else None,
            "sent_at": estimate.sent_at.isoformat() if estimate.sent_at else None,
            "valid_until": estimate.valid_until.isoformat() if estimate.valid_until else None,
            "lines": [
                {
                    "id": str(l.id),
                    "description": l.description,
                    "quantity": int(l.quantity),
                    "unit_price": float(l.unit_price),
                    "line_total": float(l.line_total),
                    "sort_order": l.sort_order,
                }
                for l in lines
            ],
        }
    }


register_tool(DESCRIPTOR, handler)
