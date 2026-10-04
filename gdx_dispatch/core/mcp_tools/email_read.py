"""MCP tool: email.read — full metadata + best-available body for one message."""
from __future__ import annotations

from typing import Any

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor
from gdx_dispatch.core.mcp_tools._helpers import agent_visible_message, coerce_uuid

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
    from gdx_dispatch.core.database import contained_read
    from gdx_dispatch.modules.outlook.models import OutlookAttachment

    mid = coerce_uuid(message_id)
    if mid is None:
        return {"error": "invalid message_id"}

    # GDXA-159: all three reads in this handler are contained. Two of them —
    # the message load and the agent privacy gate — now live in
    # `_helpers.agent_visible_message`, which carries the full rationale; the
    # third is the attachment read below. The census predicate missed the
    # first two because they sit in no `try` at all: the frame that swallows
    # is `mcp_invoke.invoke_tool`, one up.
    msg = agent_visible_message(db, mid)
    if msg is None:
        return {"error": "message not found"}

    attachments = []
    try:
        from sqlalchemy import select

        # GDXA-159 (child of GDXA-86). The `except` below degrades to "no
        # attachments", and on SQLite that is honest. On Postgres it was not:
        # a failed SELECT aborts the whole transaction, and `db` is the
        # INVOKER's session, not ours.
        #
        # What that costs, stated only as far as it was actually verified:
        # `routers/ai.py`'s ask-loop invokes every tool of one agent turn on
        # this one session (`invoke_tool(..., db=tenant_db)` inside
        # `for block in tool_use_blocks`), so one failed attachment read
        # poisoned every LATER tool call in the same turn — each of which then
        # degraded or errored naming a table that was never the problem. This
        # is the one read of the three that swallows IN THIS FRAME; see the
        # block above for the other two.
        #
        # That includes the `mcp.tool_invoke` audit row. `invoke_tool` only
        # flushes it; nothing on this path commits it except a LATER tool in
        # the same turn that calls `db.commit()`. So in a single-tool turn the
        # row is lost either way, and in a multi-tool turn it is durable only
        # if this read does not abort the transaction (measured on PG: 1 row
        # with containment, 0 and `InternalError` on commit without).
        with contained_read(db):
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
