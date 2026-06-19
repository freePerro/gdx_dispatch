"""MCP tool: estimates.create_draft — create a new draft estimate. Yellow."""
from __future__ import annotations

import secrets
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


DESCRIPTOR = ToolDescriptor(
    name="estimates.create_draft",
    description=(
        "Create a new draft estimate. Yellow tool — preview on first call, "
        "confirm to apply. customer_id required; job_id optional. The estimate "
        "is created with status=draft and no lines (use estimates.add_line)."
    ),
    blast_radius="yellow",
    approval_required=True,
    sensitivity_class="internal",
    capabilities_required=[("write", "estimate")],
    input_schema={
        "type": "object",
        "required": ["customer_id"],
        "properties": {
            "customer_id": {"type": "string"},
            "job_id": {"type": ["string", "null"]},
            "label": {"type": ["string", "null"], "maxLength": 200},
            "description": {"type": ["string", "null"]},
            "notes": {"type": ["string", "null"]},
            "jobsite_address": {"type": ["string", "null"]},
            "approval_ref": {"type": "string"},
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


def _next_estimate_number(db: Any) -> str:
    from gdx_dispatch.modules.proposals.models import Estimate

    count = db.execute(select(func.count(Estimate.id))).scalar_one() or 0
    return f"EST-{count + 1:06d}"


async def handler(
    principal: Any,
    db: Any,
    customer_id: str,
    job_id: str | None = None,
    label: str | None = None,
    description: str | None = None,
    notes: str | None = None,
    jobsite_address: str | None = None,
    approval_ref: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.models.tenant_models import Customer, Job
    from gdx_dispatch.modules.proposals.models import Estimate

    cid = _coerce_uuid(customer_id)
    if cid is None:
        return {"error": "invalid customer_id"}

    customer = db.get(Customer, cid)
    if customer is None:
        return {"error": "customer not found"}

    jid: UUID | None = None
    if job_id is not None:
        jid = _coerce_uuid(job_id)
        if jid is None:
            return {"error": "invalid job_id"}
        job = db.get(Job, jid)
        if job is None:
            return {"error": "job not found"}

    tenant_id = getattr(principal, "tenant_id", None)
    if tenant_id is None:
        return {"error": "no tenant on principal"}

    next_number = _next_estimate_number(db)

    if not approval_ref:
        return {
            "estimate": {
                "preview": True,
                "estimate_number": next_number,
                "customer_id": str(cid),
                "customer_name": customer.name,
                "job_id": str(jid) if jid else None,
                "label": label,
                "status": "draft",
            }
        }

    estimate = Estimate(
        customer_id=cid,
        job_id=jid,
        estimate_number=next_number,
        label=label,
        description=description,
        notes=notes,
        jobsite_address=jobsite_address,
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=str(tenant_id),
        status="draft",
        total=0,
    )
    db.add(estimate)
    db.commit()
    db.refresh(estimate)

    return {
        "estimate": {
            "preview": False,
            "id": str(estimate.id),
            "estimate_number": estimate.estimate_number,
            "customer_id": str(estimate.customer_id),
            "customer_name": customer.name,
            "job_id": str(estimate.job_id) if estimate.job_id else None,
            "label": estimate.label,
            "status": estimate.status,
            "total": float(estimate.total or 0),
        }
    }


register_tool(DESCRIPTOR, handler)
