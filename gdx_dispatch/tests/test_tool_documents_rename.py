"""documents.rename MCP tool contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.documents_rename  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


@pytest.mark.asyncio
async def test_renames_title():
    did = uuid4()
    doc = SimpleNamespace(id=did, title="Old", original_name="x.pdf")
    db = MagicMock()
    db.get.return_value = doc
    p = _Principal(capabilities=[("write", "document")])
    r = await invoke_tool(
        "documents.rename",
        {"document_id": str(did), "new_name": "New"},
        principal=p,
        db=db,
    )
    assert r.ok is True
    assert r.result["renamed"]["after"] == "New"
    assert doc.title == "New"


@pytest.mark.asyncio
async def test_invalid_field():
    did = uuid4()
    db = MagicMock()
    db.get.return_value = SimpleNamespace(id=did, title="x", original_name="x.pdf")
    p = _Principal(capabilities=[("write", "document")])
    r = await invoke_tool(
        "documents.rename",
        {"document_id": str(did), "new_name": "y", "field": "filename"},
        principal=p,
        db=db,
    )
    assert r.ok is True
    assert "invalid field" in r.result.get("error", "")


@pytest.mark.asyncio
async def test_capability_denied():
    did = uuid4()
    db = MagicMock()
    p = _Principal(capabilities=[("read", "document")])
    r = await invoke_tool(
        "documents.rename",
        {"document_id": str(did), "new_name": "x"},
        principal=p,
        db=db,
    )
    assert r.ok is False
