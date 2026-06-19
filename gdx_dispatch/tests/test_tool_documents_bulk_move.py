"""documents.bulk_move MCP tool — Yellow blast radius."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.documents_bulk_move  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _db_with_docs(folder, docs):
    db = MagicMock()
    db.get.return_value = folder
    scalars = MagicMock()
    scalars.scalars.return_value = iter(docs)
    db.execute.return_value = scalars
    return db


def test_descriptor_yellow():
    from gdx_dispatch.core.mcp_tools.documents_bulk_move import DESCRIPTOR

    assert DESCRIPTOR.blast_radius == "yellow"
    assert DESCRIPTOR.approval_required is True


@pytest.mark.asyncio
async def test_first_call_returns_approval_required():
    fid = uuid4()
    d1, d2 = uuid4(), uuid4()
    folder = SimpleNamespace(id=fid, name="Archive", deleted_at=None)
    docs = [
        SimpleNamespace(id=d1, folder_id=None, original_name="a.pdf"),
        SimpleNamespace(id=d2, folder_id=None, original_name="b.pdf"),
    ]
    db = _db_with_docs(folder, docs)
    p = _Principal(capabilities=[("write", "document")])
    r = await invoke_tool(
        "documents.bulk_move",
        {"document_ids": [str(d1), str(d2)], "folder_id": str(fid)},
        principal=p,
        db=db,
    )
    assert r.ok is False
    assert r.error_type == "approval_required"


@pytest.mark.asyncio
async def test_confirm_applies_bulk_move():
    fid = uuid4()
    d1, d2 = uuid4(), uuid4()
    folder = SimpleNamespace(id=fid, name="Archive", deleted_at=None)
    docs = [
        SimpleNamespace(id=d1, folder_id=None, original_name="a.pdf"),
        SimpleNamespace(id=d2, folder_id=None, original_name="b.pdf"),
    ]
    db = _db_with_docs(folder, docs)
    p = _Principal(capabilities=[("write", "document")])
    r = await invoke_tool(
        "documents.bulk_move",
        {
            "document_ids": [str(d1), str(d2)],
            "folder_id": str(fid),
            "approval_ref": "ok",
        },
        principal=p,
        db=db,
        approval_ref="ok",
    )
    assert r.ok is True, f"unexpected: {r.error_type} {r.error_body}"
    assert r.result["moved"]["preview"] is False
    assert r.result["moved"]["moved_count"] == 2
    assert docs[0].folder_id == fid
    assert docs[1].folder_id == fid


@pytest.mark.asyncio
async def test_rejects_too_many_ids():
    p = _Principal(capabilities=[("write", "document")])
    r = await invoke_tool(
        "documents.bulk_move",
        {"document_ids": [str(uuid4()) for _ in range(501)]},
        principal=p,
        db=MagicMock(),
    )
    assert r.ok is False


@pytest.mark.asyncio
async def test_rejects_invalid_uuid():
    p = _Principal(capabilities=[("write", "document")])
    r = await invoke_tool(
        "documents.bulk_move",
        {"document_ids": ["not-a-uuid"]},
        principal=p,
        db=MagicMock(),
    )
    assert r.ok is False
