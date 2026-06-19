"""documents.search MCP tool contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.documents_search  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _doc(name):
    return SimpleNamespace(
        id=uuid4(),
        filename=f"{name}.pdf",
        original_name=f"{name}.pdf",
        title=name,
        description="",
        content_type="application/pdf",
        file_size=10,
        folder_id=None,
        tags=name,
        uploaded_at=datetime.now(timezone.utc),
        deleted_at=None,
    )


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.documents_search import DESCRIPTOR

    assert DESCRIPTOR.name == "documents.search"


@pytest.mark.asyncio
async def test_search_happy_path():
    db = MagicMock()
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = [_doc("invoice2026"), _doc("estimate")]
    result.scalars.return_value = scalars
    db.execute.return_value = result

    p = _Principal(capabilities=[("read", "document")])
    r = await invoke_tool("documents.search", {"query": "invoice"}, principal=p, db=db)
    assert r.ok is True
    assert len(r.result["documents"]) == 2
