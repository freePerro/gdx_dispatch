"""Sprint 1.x-S9 — blast_radius gating in invoke_tool.

Green: applies immediately.
Yellow: first call returns 202 approval_required; second call (with
        approval_ref) applies.
Red: same as Yellow PLUS principal must hold ("admin", entity_type) or
     ("*", "*"); without admin → 403 capability_denied even with
     approval_ref.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from gdx_dispatch.core.mcp_error_schema import ERROR_APPROVAL_REQUIRED, ERROR_CAPABILITY_DENIED
from gdx_dispatch.core.mcp_invoke import invoke_tool
from gdx_dispatch.core.mcp_registry import clear_registry, register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


@dataclass
class Principal:
    tenant_id: str = "t1"
    identity_id: Any = field(default_factory=uuid4)
    capabilities: list[Any] = field(default_factory=list)


def _desc(name: str, blast_radius: str = "green", **over) -> ToolDescriptor:
    kw = dict(
        name=name,
        description=f"desc {name}",
        input_schema={
            "type": "object",
            "required": ["msg"],
            "properties": {"msg": {"type": "string"}},
        },
        output_schema={"type": "object"},
        capabilities_required=[("read", "thing")],
        blast_radius=blast_radius,
    )
    kw.update(over)
    return ToolDescriptor(**kw)


@pytest.fixture(autouse=True)
def _iso():
    clear_registry()
    yield
    clear_registry()


@pytest.mark.asyncio
async def test_green_applies_immediately():
    async def _h(**_):
        return {"status": "ok"}

    register_tool(_desc("t.green", blast_radius="green"), _h)
    p = Principal(capabilities=[("read", "thing")])
    r = await invoke_tool("t.green", {"msg": "hi"}, principal=p)
    assert r.ok is True
    assert r.result == {"status": "ok"}


@pytest.mark.asyncio
async def test_yellow_no_approval_ref_returns_202():
    async def _h(**_):  # pragma: no cover — must not run on first call
        raise AssertionError("handler should not run on yellow w/o approval_ref")

    register_tool(_desc("t.yellow", blast_radius="yellow"), _h)
    p = Principal(capabilities=[("read", "thing")])
    r = await invoke_tool("t.yellow", {"msg": "hi"}, principal=p)
    assert r.ok is False
    assert r.error_type == ERROR_APPROVAL_REQUIRED


@pytest.mark.asyncio
async def test_yellow_with_approval_ref_applies():
    async def _h(**_):
        return {"status": "ok"}

    register_tool(_desc("t.yellow", blast_radius="yellow"), _h)
    p = Principal(capabilities=[("read", "thing")])
    r = await invoke_tool("t.yellow", {"msg": "hi"}, principal=p, approval_ref="ref-123")
    assert r.ok is True
    assert r.result == {"status": "ok"}


@pytest.mark.asyncio
async def test_red_without_admin_caps_returns_capability_denied():
    async def _h(**_):  # pragma: no cover — must not run
        raise AssertionError("handler should not run for red w/o admin")

    register_tool(_desc("t.red", blast_radius="red"), _h)
    # Principal has the read cap (passes step 3) but no admin.
    p = Principal(capabilities=[("read", "thing")])
    r = await invoke_tool("t.red", {"msg": "hi"}, principal=p)
    assert r.ok is False
    assert r.error_type == ERROR_CAPABILITY_DENIED


@pytest.mark.asyncio
async def test_red_with_approval_ref_but_no_admin_still_denied():
    async def _h(**_):  # pragma: no cover — must not run
        raise AssertionError("handler should not run for red w/o admin")

    register_tool(_desc("t.red", blast_radius="red"), _h)
    p = Principal(capabilities=[("read", "thing")])
    r = await invoke_tool("t.red", {"msg": "hi"}, principal=p, approval_ref="ref-123")
    assert r.ok is False
    assert r.error_type == ERROR_CAPABILITY_DENIED


@pytest.mark.asyncio
async def test_red_with_admin_caps_and_approval_ref_applies():
    async def _h(**_):
        return {"status": "ok"}

    register_tool(_desc("t.red", blast_radius="red"), _h)
    p = Principal(capabilities=[("read", "thing"), ("admin", "t.red")])
    r = await invoke_tool("t.red", {"msg": "hi"}, principal=p, approval_ref="ref-123")
    assert r.ok is True
    assert r.result == {"status": "ok"}


@pytest.mark.asyncio
async def test_red_with_wildcard_admin_works():
    async def _h(**_):
        return {"status": "ok"}

    register_tool(_desc("t.red", blast_radius="red"), _h)
    # Security #4: the generic ("*","*") no longer clears the red gate; a real
    # admin token carries an explicit ("admin","*") alongside it.
    p = Principal(capabilities=[("read", "thing"), ("*", "*"), ("admin", "*")])
    r = await invoke_tool("t.red", {"msg": "hi"}, principal=p, approval_ref="ref-123")
    assert r.ok is True
