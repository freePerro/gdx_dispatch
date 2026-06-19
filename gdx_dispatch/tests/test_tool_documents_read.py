"""documents.read MCP tool contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.documents_read  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _mock_doc(did):
    return SimpleNamespace(
        id=did,
        filename="x.pdf",
        original_name="X.pdf",
        title="X",
        description=None,
        content_type="application/pdf",
        file_size=42,
        folder_id=None,
        customer_id=None,
        job_id=None,
        tags=None,
        uploaded_at=datetime.now(timezone.utc),
        deleted_at=None,
    )


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.documents_read import DESCRIPTOR

    assert DESCRIPTOR.name == "documents.read"
    assert ("read", "document") in [tuple(c) for c in DESCRIPTOR.capabilities_required]


@pytest.mark.asyncio
async def test_invocation_happy_path():
    did = uuid4()
    db = MagicMock()
    db.get.return_value = _mock_doc(did)
    p = _Principal(capabilities=[("read", "document")])
    r = await invoke_tool("documents.read", {"document_id": str(did)}, principal=p, db=db)
    assert r.ok is True
    assert r.result["document"]["id"] == str(did)
    assert r.result["document"]["download_url"].endswith(f"/{did}/download")


@pytest.mark.asyncio
async def test_invalid_uuid():
    db = MagicMock()
    p = _Principal(capabilities=[("read", "document")])
    r = await invoke_tool("documents.read", {"document_id": "junk"}, principal=p, db=db)
    assert r.ok is True
    assert r.result.get("error") == "invalid document_id"


@pytest.mark.asyncio
async def test_not_found():
    db = MagicMock()
    db.get.return_value = None
    p = _Principal(capabilities=[("read", "document")])
    r = await invoke_tool("documents.read", {"document_id": str(uuid4())}, principal=p, db=db)
    assert r.ok is True
    assert r.result.get("error") == "document not found"
