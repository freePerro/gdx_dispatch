"""Sprint 1.x-S36 — customers.detail MCP tool contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.get_customer_detail  # noqa: F401  — fire register_tool
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _row(cid: str, name: str = "Alice", *, deleted: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=cid,
        name=name,
        email=f"{name.lower()}@example.com",
        phone="555-0000",
        deleted_at=("2026-01-01" if deleted else None),
    )


def _mock_db_with_customer(row: SimpleNamespace | None) -> Any:
    db = MagicMock()
    db.get.return_value = row
    return db


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.get_customer_detail import DESCRIPTOR
    assert DESCRIPTOR.name == "customers.detail"
    assert DESCRIPTOR.blast_radius == "green"
    assert DESCRIPTOR.sensitivity_class == "internal"
    assert ("read", "customer") in [tuple(c) for c in DESCRIPTOR.capabilities_required]
    assert "customer_id" in DESCRIPTOR.input_schema.get("required", [])


def test_tool_registers_at_import():
    entry = get_tool("customers.detail")
    assert entry is not None
    desc, handler = entry
    assert desc.name == "customers.detail"
    assert callable(handler)


@pytest.mark.asyncio
async def test_returns_customer_when_found():
    cid = str(uuid4())
    db = _mock_db_with_customer(_row(cid, "Alice"))
    p = _Principal(capabilities=[("read", "customer")])
    r = await invoke_tool("customers.detail", {"customer_id": cid}, principal=p, db=db)
    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    out = r.result
    assert "customer" in out
    c = out["customer"]
    assert c["id"] == cid
    assert c["name"] == "Alice"
    assert c["status"] == "active"


@pytest.mark.asyncio
async def test_returns_not_found_for_unknown_customer():
    db = _mock_db_with_customer(None)
    p = _Principal(capabilities=[("read", "customer")])
    r = await invoke_tool("customers.detail", {"customer_id": "missing"}, principal=p, db=db)
    # Implementer chooses: return ok=False execution_error OR ok=True with
    # {"customer": None}. Either is acceptable; the contract is "no crash,
    # no fabricated row."
    if r.ok:
        assert r.result.get("customer") in (None, {})
    else:
        assert r.error_type is not None


@pytest.mark.asyncio
async def test_capability_denied_without_read_customer():
    cid = str(uuid4())
    db = _mock_db_with_customer(_row(cid))
    p = _Principal(capabilities=[("read", "job")])
    r = await invoke_tool("customers.detail", {"customer_id": cid}, principal=p, db=db)
    assert r.ok is False
    assert r.error_type and "capability" in r.error_type.lower()
