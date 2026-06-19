"""MCP tool: estimates.add_line — add a line to a draft estimate. Yellow."""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


DESCRIPTOR = ToolDescriptor(
    name="estimates.add_line",
    description=(
        "Add a single line to a draft estimate. Yellow tool — preview on "
        "first call, confirm to apply. Only draft estimates accept new lines."
    ),
    blast_radius="yellow",
    approval_required=True,
    sensitivity_class="internal",
    capabilities_required=[("write", "estimate")],
    input_schema={
        "type": "object",
        "required": ["estimate_id", "description", "quantity", "unit_price"],
        "properties": {
            "estimate_id": {"type": "string"},
            "description": {"type": "string", "minLength": 1},
            "quantity": {"type": "integer", "minimum": 1},
            "unit_price": {"type": "number", "minimum": 0},
            "approval_ref": {"type": "string"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "line": {"type": "object"},
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
    description: str,
    quantity: int,
    unit_price: float,
    approval_ref: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine

    eid = _coerce_uuid(estimate_id)
    if eid is None:
        return {"error": "invalid estimate_id"}

    estimate = db.get(Estimate, eid)
    if estimate is None or estimate.deleted_at is not None:
        return {"error": "estimate not found"}
    if estimate.status != "draft":
        return {"error": f"estimate is {estimate.status}, only draft is editable"}

    desc = (description or "").strip()
    if not desc:
        return {"error": "description must not be empty"}
    if quantity < 1:
        return {"error": "quantity must be >= 1"}
    if unit_price < 0:
        return {"error": "unit_price must be >= 0"}

    qty = int(quantity)
    price = Decimal(str(unit_price)).quantize(Decimal("0.01"))
    line_total = (Decimal(qty) * price).quantize(Decimal("0.01"))

    if not approval_ref:
        return {
            "line": {
                "preview": True,
                "estimate_id": str(eid),
                "estimate_number": estimate.estimate_number,
                "description": desc,
                "quantity": qty,
                "unit_price": float(price),
                "line_total": float(line_total),
            }
        }

    next_sort = (
        max((l.sort_order or 0 for l in estimate.lines), default=0) + 1
        if estimate.lines
        else 1
    )

    line = EstimateLine(
        estimate_id=eid,
        description=desc,
        quantity=qty,
        unit_price=price,
        line_total=line_total,
        sort_order=next_sort,
        company_id=str(estimate.company_id),
    )
    db.add(line)

    estimate.total = (Decimal(str(estimate.total or 0)) + line_total).quantize(Decimal("0.01"))

    db.commit()
    db.refresh(line)

    return {
        "line": {
            "preview": False,
            "id": str(line.id),
            "estimate_id": str(eid),
            "description": line.description,
            "quantity": line.quantity,
            "unit_price": float(line.unit_price),
            "line_total": float(line.line_total),
            "sort_order": line.sort_order,
            "estimate_total": float(estimate.total),
        }
    }


register_tool(DESCRIPTOR, handler)
