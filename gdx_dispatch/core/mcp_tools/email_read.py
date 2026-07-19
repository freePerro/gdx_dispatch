"""MCP tool: email.read — full metadata + best-available body for one message."""
from __future__ import annotations

from typing import Any

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.mcp_tools._helpers import coerce_uuid

DESCRIPTOR = ToolDescriptor(
    name="email.read",
    description=(
        "Fetch one email's full metadata, recipients, body preview, and "
        "attachment list. Body content is preview-truncated until R2 body "
        "storage is enabled tenant-wide."
    ),
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "email")],
    input_schema={
        "type": "object",
        "required": ["message_id"],
        "properties": {
            "message_id": {"type": "string", "description": "OutlookMessage UUID"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "message": {"type": "object"},
            "error": {"type": "string"},
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    message_id: str,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.modules.outlook.models import OutlookAttachment, OutlookMessage

    mid = coerce_uuid(message_id)
    if mid is None:
        return {"error": "invalid message_id"}

    msg = db.get(OutlookMessage, mid)
    if msg is None:
        return {"error": "message not found"}
    # Agent privacy gate — same "not found" as truly-missing, so a machine
    # caller can't probe whether a hidden message exists. (visibility.py)
    from gdx_dispatch.modules.outlook.visibility import visible_to_agent

    if not visible_to_agent(msg, db):
        return {"error": "message not found"}

    attachments = []
    try:
        from sqlalchemy import select

        rows = db.execute(
            select(OutlookAttachment).where(OutlookAttachment.message_id == mid)
        ).scalars().all()
        for a in rows:
            attachments.append(
                {
                    "id": str(a.id),
                    "filename": a.filename,
                    "content_type": a.content_type,
                    "size_bytes": a.size_bytes,
                    "is_inline": bool(a.is_inline),
                }
            )
    except Exception:  # noqa: BLE001 — attachments are best-effort
        attachments = []

    return {
        "message": {
            "id": str(msg.id),
            "subject": msg.subject,
            "from_address": msg.from_address,
            "to_addresses": msg.to_addresses,
            "cc_addresses": msg.cc_addresses,
            "bcc_addresses": msg.bcc_addresses,
            "direction": msg.direction,
            "sent_at": msg.sent_at.isoformat() if msg.sent_at else None,
            "received_at": msg.received_at.isoformat() if msg.received_at else None,
            "is_read": bool(msg.is_read),
            "has_attachments": bool(msg.has_attachments),
            "folder_id": msg.folder_id,
            "folder_display_name": msg.folder_display_name,
            "conversation_id": msg.conversation_id,
            "internet_message_id": msg.internet_message_id,
            "linked_customer_id": str(msg.linked_customer_id) if msg.linked_customer_id else None,
            "linked_job_id": str(msg.linked_job_id) if msg.linked_job_id else None,
            "body_preview": msg.body_preview,
            "body_size_bytes": msg.body_size_bytes,
            "body_storage": "preview-only" if not msg.body_r2_key else "r2",
            "attachments": attachments,
        }
    }


register_tool(DESCRIPTOR, handler)
