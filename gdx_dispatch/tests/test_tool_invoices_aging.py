"""Sprint 1.x-S40 — invoices.aging_summary contract."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4
import pytest

import gdx_dispatch.core.mcp_tools.invoices_aging  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _P:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _row(due_days_ago: int, amount: float = 100.0) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=str(uuid4()),
        amount_due=amount,
        due_date=(now - timedelta(days=due_days_ago)).date() if due_days_ago > 0 else now.date(),
        status="unpaid",
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
    from gdx_dispatch.core.mcp_tools.invoices_aging import DESCRIPTOR
    assert DESCRIPTOR.name == "invoices.aging_summary"
    assert DESCRIPTOR.description
    assert DESCRIPTOR.blast_radius == "green"
    assert ("read", "invoice") in [tuple(c) for c in DESCRIPTOR.capabilities_required]


def test_tool_registers_at_import():
    assert get_tool("invoices.aging_summary") is not None


@pytest.mark.asyncio
async def test_returns_buckets():
    rows = [_row(15, 100), _row(45, 200), _row(75, 300), _row(120, 400)]
    p = _P(capabilities=[("read", "invoice")])
    r = await invoke_tool("invoices.aging_summary", {}, principal=p, db=_mock_db(rows))
    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    assert "summary" in r.result
    buckets = {b["bucket"]: b for b in r.result["summary"]}
    # All 4 buckets must be present.
    for b in ("0-30", "31-60", "61-90", "90+"):
        assert b in buckets, f"missing bucket {b}"


@pytest.mark.asyncio
async def test_capability_denied_without_read_invoice():
    p = _P(capabilities=[("read", "customer")])
    r = await invoke_tool("invoices.aging_summary", {}, principal=p, db=_mock_db([]))
    assert r.ok is False
    assert "capability" in r.error_type.lower()
