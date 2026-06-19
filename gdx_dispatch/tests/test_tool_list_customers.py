"""Sprint 1.x-S12 — customers.list MCP tool contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

# Import the tool module to fire its register_tool() side effect once at
# test-module load. Don't reload between tests — list_customers' name is
# unique to this file and re-registering with a fresh handler would break
# the canonical "register-once" contract in mcp_registry.
import gdx_dispatch.core.mcp_tools.list_customers  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _row(name: str, *, deleted: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=str(uuid4()),
        name=name,
        email=f"{name.lower()}@example.com",
        phone=f"555-{abs(hash(name)) % 10000:04d}",
        deleted_at=("2026-01-01" if deleted else None),
    )


def _mock_db(rows: list[SimpleNamespace]) -> Any:
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = [(r,) for r in rows]
    scalars = MagicMock()
    scalars.all.return_value = rows
    result.scalars.return_value = scalars
    db.execute.return_value = result
    return db


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.list_customers import DESCRIPTOR
    assert DESCRIPTOR.name == "customers.list"
    assert DESCRIPTOR.blast_radius == "green"
    assert DESCRIPTOR.sensitivity_class == "internal"
    assert ("read", "customer") in [tuple(c) for c in DESCRIPTOR.capabilities_required]
    props = DESCRIPTOR.input_schema.get("properties", {})
    for field_name in ("name", "phone", "email", "status", "tag"):
        assert field_name in props, f"input_schema missing filter {field_name!r}"


def test_tool_registers_in_registry_at_import():
    entry = get_tool("customers.list")
    assert entry is not None
    desc, handler = entry
    assert desc.name == "customers.list"
    assert callable(handler)


@pytest.mark.asyncio
async def test_invocation_returns_customers_shape():
    rows = [_row("Alice"), _row("Bob"), _row("Charlie")]
    db = _mock_db(rows)
    p = _Principal(capabilities=[("read", "customer")])
    r = await invoke_tool("customers.list", {}, principal=p, db=db)
    assert r.ok is True, f"expected ok=True, got {r.error_type}: {r.error_body}"
    assert "customers" in r.result
    customers = r.result["customers"]
    assert isinstance(customers, list)
    for c in customers:
        assert set(["id", "name", "email", "phone", "status"]).issubset(c.keys())


@pytest.mark.asyncio
async def test_capability_denied_without_read_customer():
    db = _mock_db([_row("Alice")])
    p = _Principal(capabilities=[("read", "job")])
    r = await invoke_tool("customers.list", {}, principal=p, db=db)
    assert r.ok is False
    assert r.error_type and "capability" in r.error_type.lower()
