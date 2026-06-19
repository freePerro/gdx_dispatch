"""Regression: build_mcp_subapp must work when called from inside a
running asyncio event loop.

The lab smoke at sprint-mcp-streamable-http S6 caught a startup crash
(``asyncio.run() cannot be called from a running event loop``) because
the bridge used ``asyncio.run(mcp.list_tools())`` to enumerate already-
registered FastMCP tools. Under uvicorn factory mode (and any other
context where ``create_app`` runs inside a live loop) this raised at
import time.

The fix replaced the async list_tools call with sync access to
FastMCP's component map. This test pins that behaviour: calling
``build_mcp_subapp`` from inside ``asyncio.run(...)`` must succeed.
"""
from __future__ import annotations

import asyncio

import pytest

from fastmcp import FastMCP

from gdx_dispatch.core.mcp_fastmcp_bridge import _fastmcp_tool_names
from gdx_dispatch.core.mcp_mount import build_mcp_subapp
from gdx_dispatch.core.mcp_registry import _DESCRIPTORS, _HANDLERS, register_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor

# Trigger the canonical tool-set registration (the same side-effect import
# gdx_dispatch.app uses at startup). Other tests in this suite rely on this state,
# so we add a probe tool without clearing what's already registered.
import gdx_dispatch.core.mcp_tools  # noqa: F401, E402


@pytest.fixture()
def populated_registry():
    """Add a probe tool, then remove it on teardown (no global wipe)."""
    name = "probe.mount_under_loop"

    async def _handler(**_):
        return {"ok": True}

    register_tool(
        ToolDescriptor(
            name=name,
            description="probe",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object", "properties": {}},
            capabilities_required=[],
            blast_radius="green",
            sensitivity_class="public",
        ),
        _handler,
    )
    yield name
    _DESCRIPTORS.pop(name, None)
    _HANDLERS.pop(name, None)


def test_build_mcp_subapp_under_running_loop(populated_registry):
    """The lab failure mode: invoke from inside a running event loop."""
    mcp = FastMCP(name="test-under-loop")

    async def _go():
        # Same call create_app() makes; under uvicorn factory mode this
        # runs from inside the loop. Pre-fix this raised
        # "asyncio.run() cannot be called from a running event loop".
        return build_mcp_subapp(mcp)

    subapp = asyncio.run(_go())
    assert subapp is not None
    assert populated_registry.replace(".", "_") in _fastmcp_tool_names(mcp)


def test_fastmcp_tool_names_returns_empty_for_fresh_instance():
    mcp = FastMCP(name="empty")
    assert _fastmcp_tool_names(mcp) == set()


def test_fastmcp_tool_names_reflects_added_tools(populated_registry):
    mcp = FastMCP(name="populated")
    build_mcp_subapp(mcp)  # bridges registry → fastmcp
    assert populated_registry.replace(".", "_") in _fastmcp_tool_names(mcp)
