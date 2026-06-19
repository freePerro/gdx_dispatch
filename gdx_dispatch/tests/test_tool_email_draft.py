"""email.draft MCP tool contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.email_draft  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)
    user_id: str = field(default_factory=lambda: str(uuid4()))


def _mock_db_with_account():
    account = SimpleNamespace(id=uuid4(), upn="doug@example.com", user_id="u1")
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = account
    db.execute.return_value = result
    db.add = MagicMock()
    db.commit = MagicMock()

    def _refresh(obj):
        obj.id = uuid4()
        obj.body_preview = (obj.body_preview or "")[:255]
        obj.to_addresses = obj.to_addresses or []
        obj.cc_addresses = obj.cc_addresses or None
        obj.bcc_addresses = obj.bcc_addresses or None
        obj.subject = obj.subject or ""

    db.refresh = MagicMock(side_effect=_refresh)
    return db, account


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.email_draft import DESCRIPTOR

    assert DESCRIPTOR.name == "email.draft"
    assert DESCRIPTOR.blast_radius == "green"
    assert ("write", "email.draft") in [tuple(c) for c in DESCRIPTOR.capabilities_required]


def test_tool_registers():
    assert get_tool("email.draft") is not None


@pytest.mark.asyncio
async def test_invocation_creates_draft():
    db, _account = _mock_db_with_account()
    p = _Principal(capabilities=[("write", "email.draft")])
    r = await invoke_tool(
        "email.draft",
        {"to": ["c@x.com"], "subject": "Re: gate", "body": "Sure thing."},
        principal=p,
        db=db,
    )
    assert r.ok is True, f"unexpected: {r.error_type} {r.error_body}"
    assert r.result["draft"]["subject"] == "Re: gate"
    assert r.result["draft"]["folder"] == "Drafts"
    assert db.add.called
    assert db.commit.called


@pytest.mark.asyncio
async def test_missing_required_fields():
    db, _ = _mock_db_with_account()
    p = _Principal(capabilities=[("write", "email.draft")])
    r = await invoke_tool(
        "email.draft",
        {"to": ["c@x.com"], "subject": "", "body": "ignored"},
        principal=p,
        db=db,
    )
    # Pydantic-level field validation fires before our handler when subject
    # is empty? Actually no — JSON schema only enforces "required" presence.
    # Our handler treats empty string as missing.
    assert r.ok is True
    assert "error" in r.result


@pytest.mark.asyncio
async def test_capability_denied():
    db, _ = _mock_db_with_account()
    p = _Principal(capabilities=[("read", "email")])
    r = await invoke_tool(
        "email.draft",
        {"to": ["c@x.com"], "subject": "x", "body": "y"},
        principal=p,
        db=db,
    )
    assert r.ok is False
    assert "capability" in (r.error_type or "").lower()
