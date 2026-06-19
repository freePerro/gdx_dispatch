"""Sprint 1.x-S37 — jobs.list MCP tool contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.list_jobs  # noqa: F401  — fire register_tool
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _row(title: str = "Job", status: str = "scheduled") -> SimpleNamespace:
    return SimpleNamespace(
        id=str(uuid4()),
        title=title,
        status=status,
        customer_id=str(uuid4()),
        scheduled_at=None,
        completed_at=None,
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
    from gdx_dispatch.core.mcp_tools.list_jobs import DESCRIPTOR
    assert DESCRIPTOR.name == "jobs.list"
    assert DESCRIPTOR.blast_radius == "green"
    assert DESCRIPTOR.sensitivity_class == "internal"
    assert ("read", "job") in [tuple(c) for c in DESCRIPTOR.capabilities_required]
    props = DESCRIPTOR.input_schema.get("properties", {})
    for f in ("status", "customer_id", "technician_id", "since"):
        assert f in props, f"input_schema missing {f!r}"


def test_tool_registers_at_import():
    entry = get_tool("jobs.list")
    assert entry is not None
    desc, handler = entry
    assert desc.name == "jobs.list"
    assert callable(handler)


@pytest.mark.asyncio
async def test_returns_jobs_shape():
    rows = [_row("J1"), _row("J2"), _row("J3")]
    db = _mock_db(rows)
    p = _Principal(capabilities=[("read", "job")])
    r = await invoke_tool("jobs.list", {}, principal=p, db=db)
    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    assert "jobs" in r.result
    jobs = r.result["jobs"]
    assert isinstance(jobs, list)
    for j in jobs:
        for k in ("id", "title", "status", "customer_id"):
            assert k in j


@pytest.mark.asyncio
async def test_capability_denied_without_read_job():
    rows = [_row()]
    db = _mock_db(rows)
    p = _Principal(capabilities=[("read", "customer")])
    r = await invoke_tool("jobs.list", {}, principal=p, db=db)
    assert r.ok is False
    assert r.error_type and "capability" in r.error_type.lower()
