"""email.read MCP tool contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.email_read  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _mock_msg(mid):
    return SimpleNamespace(
        id=mid,
        is_personal=False,  # DB rows always carry the column; the agent privacy gate reads it
        subject="hi",
        from_address="alice@example.com",
        to_addresses=["doug@example.com"],
        cc_addresses=None,
        bcc_addresses=None,
        direction="inbound",
        sent_at=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
        is_read=False,
        has_attachments=False,
        folder_id="inbox",
        folder_display_name="Inbox",
        conversation_id=None,
        internet_message_id=None,
        linked_customer_id=None,
        linked_job_id=None,
        body_preview="preview",
        body_size_bytes=8,
        body_r2_key=None,
    )


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.email_read import DESCRIPTOR

    assert DESCRIPTOR.name == "email.read"
    assert ("read", "email") in [tuple(c) for c in DESCRIPTOR.capabilities_required]


def test_tool_registers():
    assert get_tool("email.read") is not None


@pytest.mark.asyncio
async def test_invocation_happy_path():
    mid = uuid4()
    db = MagicMock()
    db.get.return_value = _mock_msg(mid)
    # No OutlookSettings row → default visibility rules for the agent gate.
    db.query.return_value.filter.return_value.first.return_value = None
    # Attachment query — return empty list.
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = []
    result.scalars.return_value = scalars
    db.execute.return_value = result

    p = _Principal(capabilities=[("read", "email")])
    r = await invoke_tool("email.read", {"message_id": str(mid)}, principal=p, db=db)
    assert r.ok is True, f"unexpected: {r.error_type} {r.error_body}"
    assert r.result["message"]["id"] == str(mid)
    assert r.result["message"]["body_storage"] == "preview-only"


@pytest.mark.asyncio
async def test_invalid_uuid_returns_error():
    db = MagicMock()
    p = _Principal(capabilities=[("read", "email")])
    r = await invoke_tool("email.read", {"message_id": "not-a-uuid"}, principal=p, db=db)
    assert r.ok is True
    assert "error" in r.result


@pytest.mark.asyncio
async def test_message_not_found():
    db = MagicMock()
    db.get.return_value = None
    p = _Principal(capabilities=[("read", "email")])
    r = await invoke_tool("email.read", {"message_id": str(uuid4())}, principal=p, db=db)
    assert r.ok is True
    assert r.result.get("error") == "message not found"


@pytest.mark.asyncio
async def test_personal_message_hidden_from_agent():
    """Agent privacy gate: is_personal mail reads as 'not found' — an MCP
    caller can neither read it nor learn it exists."""
    mid = uuid4()
    msg = _mock_msg(mid)
    msg.is_personal = True
    db = MagicMock()
    db.get.return_value = msg
    db.query.return_value.filter.return_value.first.return_value = None  # default rules
    p = _Principal(capabilities=[("read", "email")])
    r = await invoke_tool("email.read", {"message_id": str(mid)}, principal=p, db=db)
    assert r.ok is True
    assert r.result.get("error") == "message not found"
