"""documents.set_tags MCP tool contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.documents_set_tags  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


@pytest.mark.asyncio
async def test_replaces_tags():
    did = uuid4()
    doc = SimpleNamespace(id=did, tags="old")
    db = MagicMock()
    db.get.return_value = doc
    p = _Principal(capabilities=[("write", "document")])
    r = await invoke_tool(
        "documents.set_tags",
        {"document_id": str(did), "tags": ["a", "b", "B"]},
        principal=p,
        db=db,
    )
    assert r.ok is True
    # Dedupes case-insensitively but preserves first-seen casing.
    assert r.result["tagged"]["after"] == "a,b"
    assert doc.tags == "a,b"


@pytest.mark.asyncio
async def test_clear_tags_with_empty_list():
    did = uuid4()
    doc = SimpleNamespace(id=did, tags="x,y")
    db = MagicMock()
    db.get.return_value = doc
    p = _Principal(capabilities=[("write", "document")])
    r = await invoke_tool(
        "documents.set_tags",
        {"document_id": str(did), "tags": []},
        principal=p,
        db=db,
    )
    assert r.ok is True
    assert doc.tags is None
