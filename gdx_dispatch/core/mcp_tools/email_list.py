"""MCP tool: email.list — search Outlook messages."""
from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


DESCRIPTOR = ToolDescriptor(
    name="email.list",
    description=(
        "Search inbox messages by sender, subject substring, folder, "
        "unread status, or date range. Returns metadata only (no body)."
    ),
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("read", "email")],
    input_schema={
        "type": "object",
        "properties": {
            "from_address": {"type": "string", "description": "Substring match on sender"},
            "subject": {"type": "string", "description": "Substring match on subject"},
            "folder_id": {"type": "string", "description": "Graph folder id; null = all folders"},
            "unread_only": {"type": "boolean", "default": False},
            "since": {"type": "string", "description": "ISO date — received_at >= this"},
            "until": {"type": "string", "description": "ISO date — received_at <= this"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "messages": {"type": "array", "items": {"type": "object"}},
            "truncated": {"type": "boolean"},
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    from_address: str | None = None,
    subject: str | None = None,
    folder_id: str | None = None,
    unread_only: bool = False,
    since: str | None = None,
    until: str | None = None,
    limit: int = 25,
    **_: Any,
) -> dict[str, Any]:
    from gdx_dispatch.modules.outlook.models import OutlookMessage
    from gdx_dispatch.modules.outlook.visibility import _load_rules, visible_to_agent

    capped_limit = max(1, min(int(limit or 25), 100))

    stmt = select(OutlookMessage)
    if from_address:
        stmt = stmt.where(OutlookMessage.from_address.ilike(f"%{from_address}%"))
    if subject:
        stmt = stmt.where(OutlookMessage.subject.ilike(f"%{subject}%"))
    if folder_id:
        stmt = stmt.where(OutlookMessage.folder_id == folder_id)
    if unread_only:
        stmt = stmt.where(OutlookMessage.is_read.is_(False))
    if since:
        stmt = stmt.where(OutlookMessage.received_at >= since)
    if until:
        stmt = stmt.where(OutlookMessage.received_at <= until)

    stmt = stmt.order_by(desc(OutlookMessage.received_at)).limit(capped_limit + 1)
    rows = list(db.execute(stmt).scalars().all())
    # Agent privacy gate: MCP principals are machine callers with no human
    # viewer identity — personal messages and fully-private (owner_only)
    # tagged mail must never reach them. Filter BEFORE the truncation cut so
    # hidden rows don't consume the page. (visibility.visible_to_agent)
    rules = _load_rules(db)
    rows = [m for m in rows if visible_to_agent(m, db, rules=rules)]
    truncated = len(rows) > capped_limit
    rows = rows[:capped_limit]

    messages = [
        {
            "id": str(m.id),
            "subject": m.subject,
            "from_address": m.from_address,
            "to_addresses": m.to_addresses,
            "received_at": m.received_at.isoformat() if m.received_at else None,
            "is_read": bool(m.is_read),
            "has_attachments": bool(m.has_attachments),
            "folder_id": m.folder_id,
            "folder_display_name": m.folder_display_name,
            "linked_customer_id": str(m.linked_customer_id) if m.linked_customer_id else None,
            "linked_job_id": str(m.linked_job_id) if m.linked_job_id else None,
            "preview": m.body_preview,
        }
        for m in rows
    ]
    return {"messages": messages, "truncated": truncated}


register_tool(DESCRIPTOR, handler)
