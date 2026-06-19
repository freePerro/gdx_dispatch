"""Sprint 1.x-S13 — customers.mark_contacted MCP tool contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.mark_customer_contacted  # noqa: F401  — register_tool side-effect
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _mock_db_with_customer(customer_id: str, *, exists: bool = True, notes: str | None = None) -> Any:
    """Build a sync Session mock that resolves db.get(Customer, id) to a row
    (or None when ``exists=False``)."""
    db = MagicMock()
    if exists:
        row = SimpleNamespace(
            id=customer_id,
            last_contacted_at=None,
            notes_appended=notes,
        )
        db.get.return_value = row
    else:
        db.get.return_value = None
    return db


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.mark_customer_contacted import DESCRIPTOR
    assert DESCRIPTOR.name == "customers.mark_contacted"
    assert DESCRIPTOR.blast_radius == "green"
    assert DESCRIPTOR.approval_required is False
    # Cap shape: write on customer.contact (matches the S2 column whitelist).
    caps = [tuple(c) for c in DESCRIPTOR.capabilities_required]
    assert ("write", "customer.contact") in caps
    # Required input field: customer_id.
    schema = DESCRIPTOR.input_schema
    assert "customer_id" in schema.get("required", [])


def test_tool_registers_at_import():
    entry = get_tool("customers.mark_contacted")
    assert entry is not None
    desc, handler = entry
    assert desc.name == "customers.mark_contacted"
    assert callable(handler)


@pytest.mark.asyncio
async def test_marks_customer_contacted_on_success():
    cid = str(uuid4())
    db = _mock_db_with_customer(cid)
    p = _Principal(capabilities=[("write", "customer.contact"), ("read", "customer")])
    r = await invoke_tool(
        "customers.mark_contacted",
        {"customer_id": cid},
        principal=p, db=db,
    )
    assert r.ok is True, f"expected ok=True, got {r.error_type}: {r.error_body}"
    # last_contacted_at is now non-None on the row the mock returned.
    row = db.get.return_value
    assert row.last_contacted_at is not None
    # commit was called.
    assert db.commit.called


@pytest.mark.asyncio
async def test_appends_note_when_supplied():
    cid = str(uuid4())
    db = _mock_db_with_customer(cid, notes="Prior note.")
    p = _Principal(capabilities=[("write", "customer.contact"), ("read", "customer")])
    r = await invoke_tool(
        "customers.mark_contacted",
        {"customer_id": cid, "note": "Followup scheduled."},
        principal=p, db=db,
    )
    assert r.ok is True
    row = db.get.return_value
    assert "Followup scheduled." in (row.notes_appended or "")
    # The previous note content is preserved (append, not replace).
    assert "Prior note." in (row.notes_appended or "")


@pytest.mark.asyncio
async def test_capability_denied_without_write_cap():
    cid = str(uuid4())
    db = _mock_db_with_customer(cid)
    p = _Principal(capabilities=[("read", "customer")])  # read-only — no write cap
    r = await invoke_tool(
        "customers.mark_contacted",
        {"customer_id": cid},
        principal=p, db=db,
    )
    assert r.ok is False
    assert r.error_type and "capability" in r.error_type.lower()


@pytest.mark.asyncio
async def test_unknown_customer_returns_input_invalid_or_execution_error():
    """When the customer doesn't exist, the tool should NOT silently no-op.
    The exact error_type is up to the implementer (input_invalid or
    execution_error are both reasonable); the contract is r.ok is False AND
    no commit fired."""
    cid = str(uuid4())
    db = _mock_db_with_customer(cid, exists=False)
    p = _Principal(capabilities=[("write", "customer.contact"), ("read", "customer")])
    r = await invoke_tool(
        "customers.mark_contacted",
        {"customer_id": cid},
        principal=p, db=db,
    )
    assert r.ok is False
    assert not db.commit.called
