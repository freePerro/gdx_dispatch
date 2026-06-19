"""documents.summarize MCP tool — text extraction contract."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.documents_summarize  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.documents_summarize import DESCRIPTOR

    assert DESCRIPTOR.name == "documents.summarize"
    assert DESCRIPTOR.blast_radius == "green"


@pytest.mark.asyncio
async def test_extracts_plaintext_file(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    sample = tmp_path / "note.txt"
    sample.write_text("hello\nworld\n", encoding="utf-8")

    did = uuid4()
    doc = SimpleNamespace(
        id=did,
        filename="note.txt",
        original_name="note.txt",
        content_type="text/plain",
        deleted_at=None,
    )
    db = MagicMock()
    db.get.return_value = doc

    p = _Principal(capabilities=[("read", "document")])
    r = await invoke_tool("documents.summarize", {"document_id": str(did)}, principal=p, db=db)
    assert r.ok is True
    extracted = r.result["extracted"]
    assert "hello" in extracted["text"] and "world" in extracted["text"]
    assert extracted["truncated"] is False


@pytest.mark.asyncio
async def test_unsupported_type_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    sample = tmp_path / "blob.bin"
    sample.write_bytes(b"\x00\x01\x02")

    did = uuid4()
    doc = SimpleNamespace(
        id=did,
        filename="blob.bin",
        original_name="blob.bin",
        content_type="application/octet-stream",
        deleted_at=None,
    )
    db = MagicMock()
    db.get.return_value = doc

    p = _Principal(capabilities=[("read", "document")])
    r = await invoke_tool("documents.summarize", {"document_id": str(did)}, principal=p, db=db)
    assert r.ok is True
    assert "unsupported" in (r.result.get("error") or "")


@pytest.mark.asyncio
async def test_deleted_document_blocked():
    did = uuid4()
    doc = SimpleNamespace(
        id=did,
        filename="x.txt",
        original_name="x.txt",
        content_type="text/plain",
        deleted_at="2026-01-01",
    )
    db = MagicMock()
    db.get.return_value = doc
    p = _Principal(capabilities=[("read", "document")])
    r = await invoke_tool("documents.summarize", {"document_id": str(did)}, principal=p, db=db)
    assert r.ok is True
    assert r.result.get("error") == "document is deleted"
