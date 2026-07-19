"""email.move MCP tool contract — Yellow blast radius."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.email_move  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _setup(folder_present: bool = True):
    mid = uuid4()
    msg = SimpleNamespace(
        id=mid,
        is_personal=False,  # DB rows always carry the column; the agent privacy gate reads it
        linked_customer_id=None,
        linked_job_id=None,
        subject="hi",
        folder_id="inbox",
        folder_display_name="Inbox",
    )
    folder = SimpleNamespace(graph_folder_id="archive", display_name="Archive") if folder_present else None
    db = MagicMock()
    db.get.return_value = msg
    # No OutlookSettings row → default visibility rules for the agent gate.
    db.query.return_value.filter.return_value.first.return_value = None
    result = MagicMock()
    result.scalar_one_or_none.return_value = folder
    db.execute.return_value = result
    db.commit = MagicMock()
    return db, msg, mid


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.email_move import DESCRIPTOR

    assert DESCRIPTOR.name == "email.move"
    assert DESCRIPTOR.blast_radius == "yellow"
    assert DESCRIPTOR.approval_required is True
    assert ("write", "email") in [tuple(c) for c in DESCRIPTOR.capabilities_required]


def test_tool_registers():
    assert get_tool("email.move") is not None


@pytest.mark.asyncio
async def test_first_call_returns_approval_required():
    db, _msg, mid = _setup()
    p = _Principal(capabilities=[("write", "email")])
    r = await invoke_tool(
        "email.move",
        {"message_id": str(mid), "target_folder_id": "archive"},
        principal=p,
        db=db,
    )
    # Yellow tools without approval_ref → invoke_tool returns approval_required.
    assert r.ok is False
    assert r.error_type == "approval_required"


@pytest.mark.asyncio
async def test_confirm_call_applies_move():
    db, msg, mid = _setup()
    p = _Principal(capabilities=[("write", "email")])
    r = await invoke_tool(
        "email.move",
        {"message_id": str(mid), "target_folder_id": "archive", "approval_ref": "approved-abc"},
        principal=p,
        db=db,
        approval_ref="approved-abc",
    )
    assert r.ok is True, f"unexpected: {r.error_type} {r.error_body}"
    assert r.result["moved"]["preview"] is False
    assert r.result["moved"]["after_folder_id"] == "archive"
    assert msg.folder_id == "archive"
    assert db.commit.called


@pytest.mark.asyncio
async def test_folder_not_found():
    db, _msg, mid = _setup(folder_present=False)
    p = _Principal(capabilities=[("write", "email")])
    r = await invoke_tool(
        "email.move",
        {"message_id": str(mid), "target_folder_id": "ghost", "approval_ref": "ok"},
        principal=p,
        db=db,
        approval_ref="ok",
    )
    assert r.ok is True
    assert "not found" in r.result.get("error", "")
