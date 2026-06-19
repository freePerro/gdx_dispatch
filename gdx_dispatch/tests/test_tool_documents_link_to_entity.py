"""documents.link_to_entity + unlink contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.documents_link_to_entity  # noqa: F401
import gdx_dispatch.core.mcp_tools.documents_unlink_from_entity  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


@pytest.mark.asyncio
async def test_link_to_customer():
    did = uuid4()
    cid = uuid4()
    doc = SimpleNamespace(id=did, customer_id=None, job_id=None)
    customer = SimpleNamespace(id=cid)
    db = MagicMock()
    # First .get() returns doc, second returns customer.
    db.get.side_effect = [doc, customer]
    p = _Principal(capabilities=[("write", "document")])
    r = await invoke_tool(
        "documents.link_to_entity",
        {"document_id": str(did), "entity_type": "customer", "entity_id": str(cid)},
        principal=p,
        db=db,
    )
    assert r.ok is True
    assert r.result["linked"]["after_customer_id"] == str(cid)
    assert doc.customer_id == cid


@pytest.mark.asyncio
async def test_unlink_clears_job_id():
    did = uuid4()
    jid = uuid4()
    doc = SimpleNamespace(id=did, customer_id=None, job_id=jid)
    db = MagicMock()
    db.get.return_value = doc
    p = _Principal(capabilities=[("write", "document")])
    r = await invoke_tool(
        "documents.unlink_from_entity",
        {"document_id": str(did), "entity_type": "job"},
        principal=p,
        db=db,
    )
    assert r.ok is True
    assert r.result["unlinked"]["before_job_id"] == str(jid)
    assert doc.job_id is None
