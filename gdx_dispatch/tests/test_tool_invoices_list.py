"""Sprint 1.x-S39 — invoices.list MCP tool contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.list_invoices  # noqa: F401  — fire register_tool
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _row(amount: float = 100.0, status: str = "unpaid") -> SimpleNamespace:
    return SimpleNamespace(
        id=str(uuid4()),
        invoice_number=f"INV-{uuid4().hex[:6]}",
        customer_id=str(uuid4()),
        status=status,
        total_amount=amount,
        amount_due=amount if status != "paid" else 0.0,
        issue_date=None,
        due_date=None,
        created_at=None,
        deleted_at=None,
    )


def _mock_db(rows: list[SimpleNamespace]) -> Any:
    db = MagicMock()
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = rows
    result.scalars.return_value = scalars
    result.all.return_value = [(r,) for r in rows]
    db.execute.return_value = result
    return db


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.list_invoices import DESCRIPTOR
    assert DESCRIPTOR.name == "invoices.list"
    assert DESCRIPTOR.description, "description is REQUIRED"
    assert DESCRIPTOR.blast_radius == "green"
    assert ("read", "invoice") in [tuple(c) for c in DESCRIPTOR.capabilities_required]
    props = DESCRIPTOR.input_schema.get("properties", {})
    for f in ("status", "customer_id", "since"):
        assert f in props


def test_tool_registers_at_import():
    entry = get_tool("invoices.list")
    assert entry is not None


@pytest.mark.asyncio
async def test_returns_invoices_shape():
    rows = [_row(100.0, "unpaid"), _row(250.0, "paid"), _row(50.0, "overdue")]
    db = _mock_db(rows)
    p = _Principal(capabilities=[("read", "invoice")])
    r = await invoke_tool("invoices.list", {}, principal=p, db=db)
    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    assert "invoices" in r.result
    invoices = r.result["invoices"]
    assert isinstance(invoices, list)
    for inv in invoices:
        for k in ("id", "invoice_number", "customer_id", "status", "total_amount"):
            assert k in inv


@pytest.mark.asyncio
async def test_capability_denied_without_read_invoice():
    db = _mock_db([_row()])
    p = _Principal(capabilities=[("read", "customer")])
    r = await invoke_tool("invoices.list", {}, principal=p, db=db)
    assert r.ok is False
    assert r.error_type and "capability" in r.error_type.lower()
