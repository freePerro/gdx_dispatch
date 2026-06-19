"""Sprint 1.x-S46 — jobs.update_status Yellow contract."""
from __future__ import annotations
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4
import pytest

import gdx_dispatch.core.mcp_tools.jobs_update_status  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _P:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _job_row(jid: str) -> SimpleNamespace:
    return SimpleNamespace(id=jid, title="J", lifecycle_stage="scheduled", deleted_at=None)


def _mock_db(job=None) -> Any:
    db = MagicMock()
    db.get.return_value = job
    return db


def test_descriptor_yellow():
    from gdx_dispatch.core.mcp_tools.jobs_update_status import DESCRIPTOR
    assert DESCRIPTOR.name == "jobs.update_status"
    assert DESCRIPTOR.description
    assert DESCRIPTOR.blast_radius == "yellow"
    caps = [tuple(c) for c in DESCRIPTOR.capabilities_required]
    assert ("write", "job") in caps or ("write", "job.status") in caps
    assert "job_id" in DESCRIPTOR.input_schema.get("required", [])
    assert "new_status" in DESCRIPTOR.input_schema.get("required", [])


def test_tool_registers():
    assert get_tool("jobs.update_status") is not None


@pytest.mark.asyncio
async def test_first_call_approval_required():
    jid = str(uuid4())
    db = _mock_db(_job_row(jid))
    p = _P(capabilities=[("write", "job"), ("write", "job.status"), ("read", "job")])
    r = await invoke_tool(
        "jobs.update_status",
        {"job_id": jid, "new_status": "completed"},
        principal=p, db=db,
    )
    assert r.ok is False
    assert r.error_type == "approval_required"


@pytest.mark.asyncio
async def test_confirm_proceeds():
    jid = str(uuid4())
    db = _mock_db(_job_row(jid))
    p = _P(capabilities=[("write", "job"), ("write", "job.status"), ("read", "job")])
    r = await invoke_tool(
        "jobs.update_status",
        {"job_id": jid, "new_status": "completed"},
        principal=p, db=db, approval_ref="tok",
    )
    assert r.error_type != "approval_required"
