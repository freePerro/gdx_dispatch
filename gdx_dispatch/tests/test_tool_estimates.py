"""estimates MCP tools — descriptor-shape and handler tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import gdx_dispatch.core.mcp_tools.estimates_add_line  # noqa: F401
import gdx_dispatch.core.mcp_tools.estimates_create_draft  # noqa: F401
import gdx_dispatch.core.mcp_tools.estimates_get  # noqa: F401
import gdx_dispatch.core.mcp_tools.estimates_list  # noqa: F401
import gdx_dispatch.core.mcp_tools.estimates_update_line  # noqa: F401
from gdx_dispatch.core.mcp_invoke import invoke_tool


@dataclass
class _Principal:
    tenant_id: str = "00000000-0000-0000-0000-000000000001"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _scalar_result(value):
    s = MagicMock()
    s.scalar_one.return_value = value
    s.scalar_one_or_none.return_value = value
    return s


def _scalars_iter(values):
    s = MagicMock()
    inner = MagicMock()
    inner.__iter__ = lambda self: iter(values)
    s.scalars.return_value = inner
    return s


def test_descriptors_have_correct_shape():
    from gdx_dispatch.core.mcp_tools.estimates_add_line import DESCRIPTOR as ADD
    from gdx_dispatch.core.mcp_tools.estimates_create_draft import DESCRIPTOR as CREATE
    from gdx_dispatch.core.mcp_tools.estimates_get import DESCRIPTOR as GET
    from gdx_dispatch.core.mcp_tools.estimates_list import DESCRIPTOR as LIST_
    from gdx_dispatch.core.mcp_tools.estimates_update_line import DESCRIPTOR as UPDATE

    assert CREATE.blast_radius == "yellow" and CREATE.approval_required is True
    assert ADD.blast_radius == "yellow" and ADD.approval_required is True
    assert UPDATE.blast_radius == "yellow" and UPDATE.approval_required is True
    assert LIST_.blast_radius == "green"
    assert GET.blast_radius == "green"


@pytest.mark.asyncio
async def test_create_draft_preview_then_apply():
    cid = uuid4()
    customer = SimpleNamespace(id=cid, name="ACME")
    db = MagicMock()
    db.get.return_value = customer
    db.execute.return_value = _scalar_result(2)

    p = _Principal(capabilities=[("write", "estimate")])

    r1 = await invoke_tool(
        "estimates.create_draft",
        {"customer_id": str(cid), "label": "test"},
        principal=p,
        db=db,
    )
    assert r1.ok is False
    assert r1.error_type == "approval_required"

    captured: dict[str, Any] = {}

    def add_side_effect(obj):
        if type(obj).__name__ == "Estimate":
            captured["estimate"] = obj
            obj.id = uuid4()
            obj.total = 0

    db.add.side_effect = add_side_effect

    r2 = await invoke_tool(
        "estimates.create_draft",
        {"customer_id": str(cid), "label": "test", "approval_ref": "ok"},
        principal=p,
        db=db,
        approval_ref="ok",
    )
    assert r2.ok is True, f"unexpected: {r2.error_type} {r2.error_body}"
    assert r2.result["estimate"]["preview"] is False
    assert r2.result["estimate"]["estimate_number"] == "EST-000003"
    assert captured["estimate"].company_id == p.tenant_id
    assert captured["estimate"].status == "draft"


@pytest.mark.asyncio
async def test_add_line_only_draft():
    eid = uuid4()
    estimate = SimpleNamespace(
        id=eid, status="sent", deleted_at=None, total=0, company_id="t1", lines=[]
    )
    db = MagicMock()
    db.get.return_value = estimate
    p = _Principal(capabilities=[("write", "estimate")])
    r = await invoke_tool(
        "estimates.add_line",
        {
            "estimate_id": str(eid),
            "description": "Spring",
            "quantity": 2,
            "unit_price": 50.0,
            "approval_ref": "ok",
        },
        principal=p,
        db=db,
        approval_ref="ok",
    )
    assert r.ok is True
    assert "only draft" in r.result["error"]


@pytest.mark.asyncio
async def test_add_line_apply_updates_total():
    eid = uuid4()
    estimate = SimpleNamespace(
        id=eid, status="draft", deleted_at=None, total=0, company_id="t1", lines=[]
    )
    db = MagicMock()
    db.get.return_value = estimate
    p = _Principal(capabilities=[("write", "estimate")])
    r = await invoke_tool(
        "estimates.add_line",
        {
            "estimate_id": str(eid),
            "description": "Spring",
            "quantity": 2,
            "unit_price": 50.0,
            "approval_ref": "ok",
        },
        principal=p,
        db=db,
        approval_ref="ok",
    )
    assert r.ok is True, f"unexpected: {r.error_type} {r.error_body}"
    assert r.result["line"]["preview"] is False
    assert r.result["line"]["line_total"] == 100.0
    assert float(estimate.total) == 100.0


@pytest.mark.asyncio
async def test_list_filters_status():
    db = MagicMock()
    db.execute.return_value = _scalars_iter([])
    p = _Principal(capabilities=[("read", "estimate")])
    r = await invoke_tool(
        "estimates.list",
        {"status": "draft", "limit": 5},
        principal=p,
        db=db,
    )
    assert r.ok is True, f"unexpected: {r.error_type} {r.error_body}"
    assert r.result["estimates"] == []


@pytest.mark.asyncio
async def test_get_returns_lines():
    eid = uuid4()
    line = SimpleNamespace(
        id=uuid4(),
        description="Spring",
        quantity=2,
        unit_price=50,
        line_total=100,
        sort_order=1,
        created_at=None,
    )
    estimate = SimpleNamespace(
        id=eid,
        estimate_number="EST-000001",
        customer_id=uuid4(),
        job_id=None,
        label="x",
        notes=None,
        jobsite_address=None,
        status="draft",
        total=100,
        created_at=None,
        sent_at=None,
        valid_until=None,
        deleted_at=None,
        lines=[line],
    )
    db = MagicMock()
    db.get.return_value = estimate
    p = _Principal(capabilities=[("read", "estimate")])
    r = await invoke_tool(
        "estimates.get",
        {"estimate_id": str(eid)},
        principal=p,
        db=db,
    )
    assert r.ok is True, f"unexpected: {r.error_type} {r.error_body}"
    assert r.result["estimate"]["estimate_number"] == "EST-000001"
    assert len(r.result["estimate"]["lines"]) == 1
