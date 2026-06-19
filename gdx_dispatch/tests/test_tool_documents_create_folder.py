"""documents.create_folder + rename_folder MCP tool contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.documents_create_folder  # noqa: F401
import gdx_dispatch.core.mcp_tools.documents_rename_folder  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)
    user_id: str = field(default_factory=lambda: str(uuid4()))


@pytest.mark.asyncio
async def test_create_folder_new():
    db = MagicMock()
    # No existing folder.
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result

    def _refresh(obj):
        obj.id = uuid4()

    db.refresh = MagicMock(side_effect=_refresh)
    db.add = MagicMock()
    db.commit = MagicMock()
    p = _Principal(capabilities=[("write", "document.folder")])
    r = await invoke_tool(
        "documents.create_folder",
        {"name": "Receipts"},
        principal=p,
        db=db,
    )
    assert r.ok is True, f"unexpected: {r.error_type} {r.error_body}"
    assert r.result["folder"]["name"] == "Receipts"
    assert r.result["folder"]["reused"] is False
    assert db.add.called


@pytest.mark.asyncio
async def test_create_folder_rejects_invalid_parent_uuid():
    db = MagicMock()
    p = _Principal(capabilities=[("write", "document.folder")])
    r = await invoke_tool(
        "documents.create_folder",
        {"name": "X", "parent_id": "not-a-uuid"},
        principal=p,
        db=db,
    )
    assert r.ok is True
    assert "valid UUID" in r.result.get("error", "")


@pytest.mark.asyncio
async def test_create_folder_rejects_unknown_parent():
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None  # parent lookup miss
    db.execute.return_value = result
    p = _Principal(capabilities=[("write", "document.folder")])
    r = await invoke_tool(
        "documents.create_folder",
        {"name": "X", "parent_id": str(uuid4())},
        principal=p,
        db=db,
    )
    assert r.ok is True
    assert "parent folder not found" in r.result.get("error", "")


@pytest.mark.asyncio
async def test_create_folder_reuses_existing():
    existing = SimpleNamespace(id=uuid4(), name="Receipts", description=None, parent_id=None)
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    db.execute.return_value = result
    p = _Principal(capabilities=[("write", "document.folder")])
    r = await invoke_tool(
        "documents.create_folder",
        {"name": "Receipts"},
        principal=p,
        db=db,
    )
    assert r.ok is True
    assert r.result["folder"]["reused"] is True


@pytest.mark.asyncio
async def test_rename_folder():
    fid = uuid4()
    folder = SimpleNamespace(id=fid, name="Old", deleted_at=None)
    db = MagicMock()
    db.get.return_value = folder
    p = _Principal(capabilities=[("write", "document.folder")])
    r = await invoke_tool(
        "documents.rename_folder",
        {"folder_id": str(fid), "new_name": "New"},
        principal=p,
        db=db,
    )
    assert r.ok is True
    assert folder.name == "New"
