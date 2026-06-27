"""MCP tool: email.move — move a message to a different folder. Yellow."""
from __future__ import annotations

from typing import Any

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.mcp_tools._helpers import coerce_uuid

DESCRIPTOR = ToolDescriptor(
    name="email.move",
    description=(
        "Move an email message to a different folder. Yellow tool — the "
        "AI assistant will preview the move (message subject + before/after "
        "folder) before applying."
    ),
    blast_radius="yellow",
    approval_required=True,
    sensitivity_class="internal",
    capabilities_required=[("write", "email")],
    input_schema={
        "type": "object",
        "required": ["message_id", "target_folder_id"],
        "properties": {
            "message_id": {"type": "string"},
            "target_folder_id": {
                "type": "string",
                "description": "Graph folder id (OutlookFolder.graph_folder_id)",
            },
            "approval_ref": {
                "type": "string",
                "description": "Echo of the approval token returned in the 202 preview response",
            },
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "moved": {"type": "object"},
            "error": {"type": "string"},
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    message_id: str,
    target_folder_id: str,
    approval_ref: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    from sqlalchemy import select

    from gdx_dispatch.modules.outlook.models import OutlookFolder, OutlookMessage

    mid = coerce_uuid(message_id)
    if mid is None:
        return {"error": "invalid message_id"}

    msg = db.get(OutlookMessage, mid)
    if msg is None:
        return {"error": "message not found"}

    folder = db.execute(
        select(OutlookFolder).where(OutlookFolder.graph_folder_id == target_folder_id).limit(1)
    ).scalar_one_or_none()
    if folder is None:
        return {"error": f"folder {target_folder_id!r} not found"}

    before_folder = msg.folder_id
    before_display = msg.folder_display_name

    # Yellow tools return a preview when no approval_ref is supplied.
    # The actual mutation runs on the confirm-call (approval_ref present).
    if not approval_ref:
        return {
            "moved": {
                "message_id": str(msg.id),
                "subject": msg.subject,
                "before_folder_id": before_folder,
                "before_folder_name": before_display,
                "after_folder_id": folder.graph_folder_id,
                "after_folder_name": folder.display_name,
                "preview": True,
            }
        }

    msg.folder_id = folder.graph_folder_id
    msg.folder_display_name = folder.display_name
    db.commit()

    return {
        "moved": {
            "message_id": str(msg.id),
            "subject": msg.subject,
            "before_folder_id": before_folder,
            "before_folder_name": before_display,
            "after_folder_id": folder.graph_folder_id,
            "after_folder_name": folder.display_name,
            "preview": False,
        }
    }


register_tool(DESCRIPTOR, handler)
