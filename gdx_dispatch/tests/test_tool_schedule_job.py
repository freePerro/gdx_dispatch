from __future__ import annotations
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4
import pytest

import gdx_dispatch.core.mcp_tools.schedule_job  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _P:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _job(jid: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=jid, title="J", scheduled_at=None, assigned_to=None,
        lifecycle_stage="estimate", deleted_at=None,
    )


def _mock_db(job=None) -> Any:
    db = MagicMock()
    db.get.return_value = job
    return db


def test_descriptor_yellow():
    from gdx_dispatch.core.mcp_tools.schedule_job import DESCRIPTOR
    assert DESCRIPTOR.name == "schedule.schedule_job"
    assert DESCRIPTOR.description
    assert DESCRIPTOR.blast_radius == "yellow"
    for f in ("job_id", "technician_id", "scheduled_at"):
        assert f in DESCRIPTOR.input_schema.get("required", [])


def test_tool_registers():
    assert get_tool("schedule.schedule_job") is not None


@pytest.mark.asyncio
async def test_first_call_approval_required():
    jid = str(uuid4())
    db = _mock_db(_job(jid))
    p = _P(capabilities=[("write", "job"), ("write", "schedule"), ("read", "job")])
    r = await invoke_tool(
        "schedule.schedule_job",
        {"job_id": jid, "technician_id": "tech-a", "scheduled_at": "2026-05-01T09:00:00Z"},
        principal=p, db=db,
    )
    assert r.ok is False
    assert r.error_type == "approval_required"


@pytest.mark.asyncio
async def test_confirm_proceeds():
    jid = str(uuid4())
    db = _mock_db(_job(jid))
    p = _P(capabilities=[("write", "job"), ("write", "schedule"), ("read", "job")])
    r = await invoke_tool(
        "schedule.schedule_job",
        {"job_id": jid, "technician_id": "tech-a", "scheduled_at": "2026-05-01T09:00:00Z"},
        principal=p, db=db, approval_ref="tok",
    )
    assert r.error_type != "approval_required"
