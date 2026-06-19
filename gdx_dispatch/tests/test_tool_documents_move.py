"""documents.move MCP tool — Green blast radius."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.documents_move  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def test_descriptor_green():
    from gdx_dispatch.core.mcp_tools.documents_move import DESCRIPTOR

    assert DESCRIPTOR.blast_radius == "green"
    assert DESCRIPTOR.approval_required is False


@pytest.mark.asyncio
async def test_move_applies_directly():
    did = uuid4()
    fid = uuid4()
    doc = SimpleNamespace(id=did, folder_id=None, original_name="x")
    folder = SimpleNamespace(id=fid, name="Archive", deleted_at=None)
    db = MagicMock()
    db.get.side_effect = [doc, folder]
    p = _Principal(capabilities=[("write", "document")])
    r = await invoke_tool(
        "documents.move",
        {"document_id": str(did), "folder_id": str(fid)},
        principal=p,
        db=db,
    )
    assert r.ok is True, f"unexpected: {r.error_type} {r.error_body}"
    assert r.result["moved"]["after_folder_id"] == str(fid)
    assert doc.folder_id == fid
