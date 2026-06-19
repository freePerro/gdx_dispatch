"""MCP tool: estimates.list — list estimates with optional filters. Green."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


_VALID_STATUSES = {"draft", "sent", "accepted", "declined", "rejected", "expired"}


DESCRIPTOR = ToolDescriptor(
    name="estimates.list",
    description=(
        "List estimates, optionally filtered by status or customer_id. "
        "Returns most-recent first. Default limit 25, max 100."
    ),
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "estimate")],
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": ["string", "null"],
                "enum": [None, "draft", "sent", "accepted", "declined", "rejected", "expired"],
            },
            "customer_id": {"type": ["string", "null"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "estimates": {"type": "array", "items": {"type": "object"}},
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
    status: str | None = None,
    customer_id: str | None = None,
    limit: int = 25,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.modules.proposals.models import Estimate

    if status is not None and status not in _VALID_STATUSES:
        return {"error": f"invalid status; must be one of {sorted(_VALID_STATUSES)}"}

    cid: UUID | None = None
    if customer_id is not None:
        cid = _coerce_uuid(customer_id)
        if cid is None:
            return {"error": "invalid customer_id"}

    lim = max(1, min(100, int(limit) if limit else 25))

    stmt = select(Estimate).where(Estimate.deleted_at.is_(None))
    if status is not None:
        stmt = stmt.where(Estimate.status == status)
    if cid is not None:
        stmt = stmt.where(Estimate.customer_id == cid)
    stmt = stmt.order_by(Estimate.created_at.desc()).limit(lim)

    rows = list(db.execute(stmt).scalars())
    return {
        "estimates": [
            {
                "id": str(e.id),
                "estimate_number": e.estimate_number,
                "customer_id": str(e.customer_id) if e.customer_id else None,
                "job_id": str(e.job_id) if e.job_id else None,
                "label": e.label,
                "status": e.status,
                "total": float(e.total or 0),
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in rows
        ]
    }


register_tool(DESCRIPTOR, handler)
