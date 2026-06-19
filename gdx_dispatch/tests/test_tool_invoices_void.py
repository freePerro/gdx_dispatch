from __future__ import annotations
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4
import pytest

import gdx_dispatch.core.mcp_tools.invoices_void  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _P:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _inv(iid: str, status: str = "unpaid") -> SimpleNamespace:
    return SimpleNamespace(id=iid, status=status, total_amount=100.0, deleted_at=None)


def _mock_db(inv=None) -> Any:
    db = MagicMock()
    db.get.return_value = inv
    return db


def test_descriptor_red():
    from gdx_dispatch.core.mcp_tools.invoices_void import DESCRIPTOR
    assert DESCRIPTOR.name == "invoices.void"
    assert DESCRIPTOR.description
    assert DESCRIPTOR.blast_radius == "red"
    assert "invoice_id" in DESCRIPTOR.input_schema.get("required", [])


def test_tool_registers():
    assert get_tool("invoices.void") is not None


@pytest.mark.asyncio
async def test_red_without_admin_denied():
    iid = str(uuid4())
    db = _mock_db(_inv(iid))
    # Has write cap but NOT admin — red gate denies regardless.
    p = _P(capabilities=[("write", "invoice"), ("read", "invoice")])
    r = await invoke_tool("invoices.void", {"invoice_id": iid}, principal=p, db=db, approval_ref="tok")
    assert r.ok is False
    assert "capability" in r.error_type.lower()


@pytest.mark.asyncio
async def test_red_with_admin_and_approval_ref_proceeds():
    iid = str(uuid4())
    db = _mock_db(_inv(iid))
    p = _P(capabilities=[
        ("write", "invoice"), ("read", "invoice"),
        ("admin", "invoices.void"),
    ])
    r = await invoke_tool(
        "invoices.void", {"invoice_id": iid},
        principal=p, db=db, approval_ref="tok",
    )
    # Red admin pass + approval_ref set → handler runs; result not approval_required, not capability_denied.
    assert r.error_type != "approval_required"
    assert r.error_type is None or "capability" not in (r.error_type or "").lower()


@pytest.mark.asyncio
async def test_red_generic_wildcard_no_longer_admin():
    # Security #4: the broad ("*","*") cap that every mcp:invoke token carries
    # must NOT clear the red-tool admin gate — only an explicit admin cap may.
    iid = str(uuid4())
    db = _mock_db(_inv(iid))
    p = _P(capabilities=[("*", "*")])
    r = await invoke_tool(
        "invoices.void", {"invoice_id": iid},
        principal=p, db=db, approval_ref="tok",
    )
    assert r.ok is False
    assert "capability" in (r.error_type or "").lower()


@pytest.mark.asyncio
async def test_red_with_admin_wildcard_works():
    iid = str(uuid4())
    db = _mock_db(_inv(iid))
    # A real admin token carries mcp:invoke (→ ("*","*"), satisfies the normal
    # write/read caps) AND mcp:admin (→ ("admin","*"), clears the red gate).
    p = _P(capabilities=[("*", "*"), ("admin", "*")])
    r = await invoke_tool(
        "invoices.void", {"invoice_id": iid},
        principal=p, db=db, approval_ref="tok",
    )
    assert r.error_type != "approval_required"
    assert r.error_type is None or "capability" not in (r.error_type or "").lower()
