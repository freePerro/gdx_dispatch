"""MCP tool: pat.rotate — rotate a Personal Access Token.

Sensitivity: ``restricted``. Sensitive PAT operations reuse the SS-15
approval gate pattern: the handler returns a ``pending_approval``
result and the actual rotation is completed only after a tenant-admin
approves via the SS-15 admin_pats approval endpoint.

INTEGRATION TODO
----------------
* wire through to ``gdx_dispatch.routers.auth.admin_pats`` approval flow + the SS-15
  ``AccessToken.status`` / ``metadata_json`` columns once the
  integration migration lands.
* today the handler stages a request record via the SS-18 additions
  stub (tool_execution_audit) — see ``gdx_dispatch.models.platform_ss18_additions``.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from gdx_dispatch.core.mcp_registry import register_tool, require_capability
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor

DESCRIPTOR = ToolDescriptor(
    name="pat.rotate",
    description="Rotate a Personal Access Token. Requires tenant-admin approval before the rotation completes.",
    input_schema={
        "type": "object",
        "required": ["pat_id"],
        "properties": {
            "pat_id": {"type": "string", "description": "UUID of the PAT to rotate"},
            "reason": {"type": "string", "description": "Why the PAT is being rotated (audit log)"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "rotation_id": {"type": "string"},
            "status": {"type": "string", "enum": ["pending_approval", "active"]},
            "requires_approval": {"type": "boolean"},
        },
    },
    capabilities_required=[("admin", "pat")],
    sensitivity_class="restricted",
    approval_required=True,
)


async def handler(
    *,
    pat_id: str,
    reason: str | None = None,
    principal: Any = None,
    db: Any = None,
    **_ignored: Any,
) -> dict[str, Any]:
    """Stage a PAT rotation. Returns pending_approval (SS-15 pattern).

    The real rotation is performed by the admin-approved
    ``POST /api/admin/pats/{id}/approve`` endpoint. Here we only stage
    the intent and return the rotation_id + status for the caller to
    poll.
    """
    require_capability(principal, DESCRIPTOR)
    if not pat_id:
        raise ValueError("pat_id is required")

    rotation_id = str(uuid4())
    # A real implementation writes to tool_execution_audit with
    # status='pending_approval' and emits an event so a human approver
    # can action it. Stubbed today until the SS-18 additions migration
    # lands; the shape is stable for the SS-19 transport adapter.
    return {
        "rotation_id": rotation_id,
        "status": "pending_approval",
        "requires_approval": True,
        "pat_id": pat_id,
        "reason": reason,
    }


register_tool(DESCRIPTOR, handler)
