"""MCP tool: estimates.update_line — update a line on a draft estimate. Yellow."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.mcp_tools._helpers import coerce_uuid

DESCRIPTOR = ToolDescriptor(
    name="estimates.update_line",
    description=(
        "Update a line on a draft estimate. Yellow tool — preview on first "
        "call, confirm to apply. Any of description/quantity/unit_price may be "
        "passed; omitted fields are unchanged. Only draft estimates are editable."
    ),
    blast_radius="yellow",
    approval_required=True,
    sensitivity_class="internal",
    capabilities_required=[("write", "estimate")],
    input_schema={
        "type": "object",
        "required": ["line_id"],
        "properties": {
            "line_id": {"type": "string"},
            "description": {"type": ["string", "null"], "minLength": 1},
            "quantity": {"type": ["integer", "null"], "minimum": 1},
            "unit_price": {"type": ["number", "null"], "minimum": 0},
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


async def handler(
    principal: Any,
    db: Any,
    line_id: str,
    description: str | None = None,
    quantity: int | None = None,
    unit_price: float | None = None,
    approval_ref: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine

    lid = coerce_uuid(line_id)
    if lid is None:
        return {"error": "invalid line_id"}

    line = db.get(EstimateLine, lid)
    if line is None:
        return {"error": "line not found"}

    estimate = db.get(Estimate, line.estimate_id)
    if estimate is None or estimate.deleted_at is not None:
        return {"error": "estimate not found"}
    if estimate.status != "draft":
        return {"error": f"estimate is {estimate.status}, only draft is editable"}

    new_desc = line.description
    if description is not None:
        cleaned = description.strip()
        if not cleaned:
            return {"error": "description must not be empty"}
        new_desc = cleaned

    new_qty = int(quantity) if quantity is not None else int(line.quantity)
    if new_qty < 1:
        return {"error": "quantity must be >= 1"}

    new_price = (
        Decimal(str(unit_price)).quantize(Decimal("0.01"))
        if unit_price is not None
        else Decimal(str(line.unit_price))
    )
    if new_price < 0:
        return {"error": "unit_price must be >= 0"}

    new_total = (Decimal(new_qty) * new_price).quantize(Decimal("0.01"))
    old_total = Decimal(str(line.line_total or 0))

    if not approval_ref:
        return {
            "line": {
                "preview": True,
                "id": str(line.id),
                "before": {
                    "description": line.description,
                    "quantity": int(line.quantity),
                    "unit_price": float(line.unit_price),
                    "line_total": float(line.line_total),
                },
                "after": {
                    "description": new_desc,
                    "quantity": new_qty,
                    "unit_price": float(new_price),
                    "line_total": float(new_total),
                },
            }
        }

    line.description = new_desc
    line.quantity = new_qty
    line.unit_price = new_price
    line.line_total = new_total

    estimate.total = (
        Decimal(str(estimate.total or 0)) - old_total + new_total
    ).quantize(Decimal("0.01"))

    db.commit()

    return {
        "line": {
            "preview": False,
            "id": str(line.id),
            "description": line.description,
            "quantity": int(line.quantity),
            "unit_price": float(line.unit_price),
            "line_total": float(line.line_total),
            "estimate_total": float(estimate.total),
        }
    }


register_tool(DESCRIPTOR, handler)
