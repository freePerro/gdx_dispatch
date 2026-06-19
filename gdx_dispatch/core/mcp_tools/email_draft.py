"""MCP tool: email.draft — create an Outlook draft. Never sends."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from gdx_dispatch.core.mcp_registry import register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


DESCRIPTOR = ToolDescriptor(
    name="email.draft",
    description=(
        "Create a draft email message. The draft is stored locally and shown "
        "to the user in the Drafts folder; nothing is sent. Use email.send "
        "(not yet enabled) to actually transmit."
    ),
    blast_radius="green",
    sensitivity_class="internal",
    capabilities_required=[("write", "email.draft")],
    input_schema={
        "type": "object",
        "required": ["to", "subject", "body"],
        "properties": {
            "to": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Recipient email addresses",
            },
            "cc": {"type": "array", "items": {"type": "string"}},
            "bcc": {"type": "array", "items": {"type": "string"}},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "in_reply_to_message_id": {
                "type": "string",
                "description": "Optional OutlookMessage UUID this drafts a reply to",
            },
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "draft": {"type": "object"},
            "error": {"type": "string"},
        },
    },
)


async def handler(
    principal: Any,
    db: Any,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    in_reply_to_message_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    from sqlalchemy import select

    from gdx_dispatch.modules.outlook.models import OutlookAccount, OutlookMessage

    if not to or not subject or not body:
        return {"error": "to, subject, and body are required"}

    user_id = None
    for attr in ("user_id", "id", "sub"):
        v = getattr(principal, attr, None)
        if v:
            user_id = str(v)
            break

    account = None
    if user_id:
        account = db.execute(
            select(OutlookAccount).where(OutlookAccount.user_id == user_id).limit(1)
        ).scalar_one_or_none()
    if account is None:
        # Fall back to any account on the tenant — single-mailbox tenants
        # are the common case while we ship per-user accounts.
        account = db.execute(select(OutlookAccount).limit(1)).scalar_one_or_none()
    if account is None:
        return {"error": "no Outlook account connected for this tenant"}

    in_reply_to_internet_id: str | None = None
    if in_reply_to_message_id:
        try:
            from uuid import UUID

            parent = db.get(OutlookMessage, UUID(str(in_reply_to_message_id)))
            if parent is not None:
                in_reply_to_internet_id = parent.internet_message_id
        except (ValueError, TypeError):
            pass

    draft = OutlookMessage(
        account_id=account.id,
        graph_message_id=f"local-draft-{uuid4()}",
        subject=subject,
        from_address=account.upn,
        to_addresses=list(to),
        cc_addresses=list(cc) if cc else None,
        bcc_addresses=list(bcc) if bcc else None,
        direction="outbound",
        body_preview=(body or "")[:255],
        body_size_bytes=len(body or ""),
        is_read=True,
        folder_id="drafts",
        folder_display_name="Drafts",
        in_reply_to=in_reply_to_internet_id,
        sent_at=None,
        received_at=datetime.now(timezone.utc),
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)

    return {
        "draft": {
            "id": str(draft.id),
            "subject": draft.subject,
            "to": draft.to_addresses,
            "cc": draft.cc_addresses,
            "bcc": draft.bcc_addresses,
            "body_preview": draft.body_preview,
            "folder": "Drafts",
            "in_reply_to": in_reply_to_internet_id,
        }
    }


register_tool(DESCRIPTOR, handler)
