"""Sprint 1.x-S45 — invoices.create_draft Yellow contract."""
from __future__ import annotations
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4
import pytest

import gdx_dispatch.core.mcp_tools.invoices_create_draft  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import get_tool


@dataclass
class _P:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _job_row(jid: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=jid,
        title="Test Job",
        customer_id=str(uuid4()),
        lifecycle_stage="completed",
        deleted_at=None,
    )


def _mock_db(job: SimpleNamespace | None = None) -> Any:
    db = MagicMock()
    db.get.return_value = job
    return db


def test_descriptor_is_yellow():
    from gdx_dispatch.core.mcp_tools.invoices_create_draft import DESCRIPTOR
    assert DESCRIPTOR.name == "invoices.create_draft"
    assert DESCRIPTOR.description
    assert DESCRIPTOR.blast_radius == "yellow"
    caps = [tuple(c) for c in DESCRIPTOR.capabilities_required]
    assert ("write", "invoice") in caps or ("write", "invoice.draft") in caps


def test_tool_registers():
    assert get_tool("invoices.create_draft") is not None


@pytest.mark.asyncio
async def test_first_call_returns_approval_required():
    """Yellow tool, no approval_ref → invoke_tool returns approval_required."""
    jid = str(uuid4())
    db = _mock_db(_job_row(jid))
    p = _P(capabilities=[("write", "invoice"), ("write", "invoice.draft"), ("read", "job")])
    r = await invoke_tool("invoices.create_draft", {"job_id": jid}, principal=p, db=db)
    assert r.ok is False
    assert r.error_type == "approval_required"


@pytest.mark.asyncio
async def test_confirm_call_proceeds_to_handler():
    """Yellow tool with approval_ref skips the gate and calls the handler."""
    jid = str(uuid4())
    db = _mock_db(_job_row(jid))
    p = _P(capabilities=[("write", "invoice"), ("write", "invoice.draft"), ("read", "job")])
    r = await invoke_tool(
        "invoices.create_draft",
        {"job_id": jid},
        principal=p, db=db, approval_ref="tok_abc",
    )
    # On confirm: handler ran. Result may be ok or fail-with-different-error
    # (e.g. mock db can't fully simulate insert). Contract: NOT approval_required.
    assert r.error_type != "approval_required"
