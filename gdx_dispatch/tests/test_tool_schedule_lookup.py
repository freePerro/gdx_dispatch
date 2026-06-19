"""Sprint 1.x-S44 — schedule.lookup contract."""
from __future__ import annotations
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4
import pytest

import gdx_dispatch.core.mcp_tools.schedule_lookup  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _P:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _row(scheduled_at, title="Service", tech="t1") -> SimpleNamespace:
    return SimpleNamespace(
        id=str(uuid4()),
        title=title,
        scheduled_at=scheduled_at,
        customer_id=str(uuid4()),
        assigned_to=tech,
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
    from gdx_dispatch.core.mcp_tools.schedule_lookup import DESCRIPTOR
    assert DESCRIPTOR.name == "schedule.lookup"
    assert DESCRIPTOR.description
    assert DESCRIPTOR.blast_radius == "green"
    caps = [tuple(c) for c in DESCRIPTOR.capabilities_required]
    assert any(c in caps for c in [("read", "schedule"), ("read", "job")])


def test_tool_registers():
    assert get_tool("schedule.lookup") is not None


@pytest.mark.asyncio
async def test_returns_schedule_shape():
    from datetime import datetime, timezone
    rows = [_row(datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc), "Job A"),
            _row(datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc), "Job B")]
    p = _P(capabilities=[("read", "job"), ("read", "schedule")])
    r = await invoke_tool("schedule.lookup", {}, principal=p, db=_mock_db(rows))
    assert r.ok is True, f"{r.error_type}: {r.error_body}"
    assert "schedule" in r.result
    sched = r.result["schedule"]
    assert isinstance(sched, list)
    for entry in sched:
        for k in ("job_id", "title", "scheduled_at"):
            assert k in entry


@pytest.mark.asyncio
async def test_capability_denied():
    p = _P(capabilities=[("read", "customer")])
    r = await invoke_tool("schedule.lookup", {}, principal=p, db=_mock_db([]))
    assert r.ok is False
    assert "capability" in r.error_type.lower()
