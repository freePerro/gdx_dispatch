"""Sprint 1.x-S42 — technicians.activity contract."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4
import pytest

import gdx_dispatch.core.mcp_tools.technicians_activity  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _P:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _mock_rows(rows: list[tuple]) -> Any:
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    db.execute.return_value = result
    return db


def test_descriptor_shape():
    from gdx_dispatch.core.mcp_tools.technicians_activity import DESCRIPTOR
    assert DESCRIPTOR.name == "technicians.activity"
    assert DESCRIPTOR.description
    assert DESCRIPTOR.blast_radius == "green"
    caps = [tuple(c) for c in DESCRIPTOR.capabilities_required]
    # Either ("read","technician") or ("read","job") satisfies; tool author picks one.
    assert any(c in caps for c in [("read", "technician"), ("read", "job")])


def test_tool_registers_at_import():
    assert get_tool("technicians.activity") is not None


@pytest.mark.asyncio
async def test_returns_technicians_shape():
    # Mock returns rows shaped (tech_id, completed_count, in_progress_count, last_active)
    rows = [("tech-a", 5, 2, "2026-04-25"), ("tech-b", 3, 1, "2026-04-24")]
    p = _P(capabilities=[("read", "job"), ("read", "technician")])
    r = await invoke_tool("technicians.activity", {}, principal=p, db=_mock_rows(rows))
    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    assert "technicians" in r.result
    techs = r.result["technicians"]
    assert isinstance(techs, list)
    for t in techs:
        for k in ("technician_id", "jobs_completed", "jobs_in_progress"):
            assert k in t


@pytest.mark.asyncio
async def test_capability_denied():
    p = _P(capabilities=[("read", "customer")])
    r = await invoke_tool("technicians.activity", {}, principal=p, db=_mock_rows([]))
    assert r.ok is False
    assert "capability" in r.error_type.lower()
