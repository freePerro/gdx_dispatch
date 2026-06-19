"""email.list MCP tool contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.email_list  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _msg(subject: str, *, from_addr: str = "alice@example.com", folder: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        subject=subject,
        from_address=from_addr,
        to_addresses=["doug@example.com"],
        received_at=datetime.now(timezone.utc),
        is_read=False,
        has_attachments=False,
        folder_id=folder,
        folder_display_name=folder,
        linked_customer_id=None,
        linked_job_id=None,
        body_preview=f"preview of {subject}",
    )


def _mock_db(rows: list[SimpleNamespace]) -> Any:
    db = MagicMock()
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = rows
    result.scalars.return_value = scalars
    db.execute.return_value = result
    return db


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.email_list import DESCRIPTOR

    assert DESCRIPTOR.name == "email.list"
    assert DESCRIPTOR.blast_radius == "green"
    assert ("read", "email") in [tuple(c) for c in DESCRIPTOR.capabilities_required]


def test_tool_registers():
    assert get_tool("email.list") is not None


@pytest.mark.asyncio
async def test_invocation_happy_path():
    rows = [_msg("hello"), _msg("world")]
    db = _mock_db(rows)
    p = _Principal(capabilities=[("read", "email")])
    r = await invoke_tool("email.list", {"unread_only": True}, principal=p, db=db)
    assert r.ok is True, f"unexpected error: {r.error_type} {r.error_body}"
    assert "messages" in r.result
    assert len(r.result["messages"]) == 2
    assert r.result["messages"][0]["subject"] in {"hello", "world"}


@pytest.mark.asyncio
async def test_capability_denied_without_read_email():
    db = _mock_db([])
    p = _Principal(capabilities=[("read", "customer")])
    r = await invoke_tool("email.list", {}, principal=p, db=db)
    assert r.ok is False
    assert r.error_type and "capability" in r.error_type.lower()
