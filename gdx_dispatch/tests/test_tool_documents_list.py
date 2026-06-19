"""documents.list MCP tool contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.documents_list  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _doc(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        filename=f"{name}.pdf",
        original_name=f"{name}.pdf",
        title=name,
        description=None,
        content_type="application/pdf",
        file_size=1024,
        folder_id=None,
        customer_id=None,
        job_id=None,
        tags="invoice,2026",
        uploaded_at=datetime.now(timezone.utc),
        deleted_at=None,
    )


def _mock_db(rows):
    db = MagicMock()
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = rows
    result.scalars.return_value = scalars
    db.execute.return_value = result
    return db


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.documents_list import DESCRIPTOR

    assert DESCRIPTOR.name == "documents.list"
    assert DESCRIPTOR.blast_radius == "green"
    assert ("read", "document") in [tuple(c) for c in DESCRIPTOR.capabilities_required]


def test_tool_registers():
    assert get_tool("documents.list") is not None


@pytest.mark.asyncio
async def test_invocation_returns_documents():
    db = _mock_db([_doc("a"), _doc("b")])
    p = _Principal(capabilities=[("read", "document")])
    r = await invoke_tool("documents.list", {}, principal=p, db=db)
    assert r.ok is True, f"unexpected: {r.error_type} {r.error_body}"
    assert len(r.result["documents"]) == 2
    assert r.result["documents"][0]["tags"] == "invoice,2026"


@pytest.mark.asyncio
async def test_invalid_folder_id():
    db = _mock_db([])
    p = _Principal(capabilities=[("read", "document")])
    r = await invoke_tool("documents.list", {"folder_id": "junk"}, principal=p, db=db)
    assert r.ok is True
    assert "error" in r.result


@pytest.mark.asyncio
async def test_capability_denied():
    db = _mock_db([])
    p = _Principal(capabilities=[("read", "customer")])
    r = await invoke_tool("documents.list", {}, principal=p, db=db)
    assert r.ok is False
    assert "capability" in (r.error_type or "").lower()
