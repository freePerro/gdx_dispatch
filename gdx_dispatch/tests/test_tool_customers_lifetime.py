"""Sprint 1.x-S43 — customers.lifetime_analysis contract."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4
import pytest

import gdx_dispatch.core.mcp_tools.customers_lifetime  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _P:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _mock_db_aggregate(total_paid: float, count: int, first_date, last_date) -> Any:
    db = MagicMock()
    result = MagicMock()
    result.first.return_value = (total_paid, count, first_date, last_date)
    result.scalar_one.return_value = (total_paid, count, first_date, last_date)
    db.execute.return_value = result
    return db


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.customers_lifetime import DESCRIPTOR
    assert DESCRIPTOR.name == "customers.lifetime_analysis"
    assert DESCRIPTOR.description
    assert DESCRIPTOR.blast_radius == "green"
    caps = [tuple(c) for c in DESCRIPTOR.capabilities_required]
    assert ("read", "customer") in caps
    assert "customer_id" in DESCRIPTOR.input_schema.get("required", [])


def test_tool_registers():
    assert get_tool("customers.lifetime_analysis") is not None


@pytest.mark.asyncio
async def test_returns_lifetime_shape():
    cid = str(uuid4())
    db = _mock_db_aggregate(5000.00, 12, "2024-01-15", "2026-04-20")
    p = _P(capabilities=[("read", "customer"), ("read", "invoice")])
    r = await invoke_tool("customers.lifetime_analysis", {"customer_id": cid}, principal=p, db=db)
    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    assert "lifetime" in r.result
    lt = r.result["lifetime"]
    for k in ("customer_id", "total_paid", "invoice_count"):
        assert k in lt


@pytest.mark.asyncio
async def test_capability_denied():
    p = _P(capabilities=[("read", "job")])
    r = await invoke_tool("customers.lifetime_analysis", {"customer_id": str(uuid4())},
                          principal=p, db=_mock_db_aggregate(0, 0, None, None))
    assert r.ok is False
    assert "capability" in r.error_type.lower()
