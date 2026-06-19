"""Sprint 1.x-S38 — jobs.detail MCP tool contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.get_job_detail  # noqa: F401  — fire register_tool
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _row(jid: str, *, completed: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=jid,
        title="Replace spring",
        lifecycle_stage=("completed" if completed else "scheduled"),
        status=None,
        customer_id=str(uuid4()),
        scheduled_at=None,
        completed_at=None,
        deleted_at=None,
    )


def _mock_db(row: SimpleNamespace | None) -> Any:
    db = MagicMock()
    db.get.return_value = row
    return db


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.get_job_detail import DESCRIPTOR
    assert DESCRIPTOR.name == "jobs.detail"
    assert DESCRIPTOR.description, "description is REQUIRED on ToolDescriptor"
    assert DESCRIPTOR.blast_radius == "green"
    assert ("read", "job") in [tuple(c) for c in DESCRIPTOR.capabilities_required]
    assert "job_id" in DESCRIPTOR.input_schema.get("required", [])


def test_tool_registers_at_import():
    entry = get_tool("jobs.detail")
    assert entry is not None
    desc, _ = entry
    assert desc.name == "jobs.detail"


@pytest.mark.asyncio
async def test_returns_job_when_found():
    jid = str(uuid4())
    db = _mock_db(_row(jid))
    p = _Principal(capabilities=[("read", "job")])
    r = await invoke_tool("jobs.detail", {"job_id": jid}, principal=p, db=db)
    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    assert "job" in r.result
    j = r.result["job"]
    assert j["id"] == jid
    assert j["lifecycle_stage"] == "scheduled"


@pytest.mark.asyncio
async def test_returns_not_found_for_missing_job():
    db = _mock_db(None)
    p = _Principal(capabilities=[("read", "job")])
    r = await invoke_tool("jobs.detail", {"job_id": "missing"}, principal=p, db=db)
    if r.ok:
        assert r.result.get("job") in (None, {})
    else:
        assert r.error_type is not None


@pytest.mark.asyncio
async def test_capability_denied_without_read_job():
    db = _mock_db(_row(str(uuid4())))
    p = _Principal(capabilities=[("read", "customer")])
    r = await invoke_tool("jobs.detail", {"job_id": "any"}, principal=p, db=db)
    assert r.ok is False
    assert r.error_type and "capability" in r.error_type.lower()
