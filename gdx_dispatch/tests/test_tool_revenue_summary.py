"""Sprint 1.x-S41 — revenue.summary contract."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4
import pytest

import gdx_dispatch.core.mcp_tools.revenue_summary  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _P:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _mock_db_total(total: float, count: int) -> Any:
    db = MagicMock()
    # The handler may call db.execute(select(sum(...))).scalar() or similar.
    # Provide both shapes.
    result = MagicMock()
    result.scalar.return_value = total
    result.scalar_one.return_value = (total, count)
    result.first.return_value = (total, count)
    result.all.return_value = [(total, count)]
    db.execute.return_value = result
    return db


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.revenue_summary import DESCRIPTOR
    assert DESCRIPTOR.name == "revenue.summary"
    assert DESCRIPTOR.description
    assert DESCRIPTOR.blast_radius == "green"
    assert ("read", "invoice") in [tuple(c) for c in DESCRIPTOR.capabilities_required]


def test_tool_registers_at_import():
    assert get_tool("revenue.summary") is not None


@pytest.mark.asyncio
async def test_returns_revenue_shape():
    p = _P(capabilities=[("read", "invoice")])
    r = await invoke_tool("revenue.summary", {}, principal=p, db=_mock_db_total(1234.56, 7))
    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    assert "revenue" in r.result
    rev = r.result["revenue"]
    for k in ("total", "invoice_count", "window"):
        assert k in rev


@pytest.mark.asyncio
async def test_capability_denied():
    p = _P(capabilities=[("read", "customer")])
    r = await invoke_tool("revenue.summary", {}, principal=p, db=_mock_db_total(0, 0))
    assert r.ok is False
    assert "capability" in r.error_type.lower()
