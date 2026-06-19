"""Sprint MCP-Streamable-HTTP S1 — bridge `mcp_registry` → FastMCP.

Acceptance: every tool registered with the legacy ``mcp_registry`` is
also reachable through the FastMCP singleton after the bridge runs.
S2 mounts the FastMCP ASGI sub-app at ``/mcp``; that's where the
``tools/list`` shape is verified end-to-end. Here we verify the
in-process registration shape only.
"""
from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP

import gdx_dispatch.core.mcp_tools  # noqa: F401  — side-effect: registers tools
from gdx_dispatch.core.mcp_fastmcp_bridge import (
    BridgeRegistrationError,
    PrincipalResolutionFailed,
    _make_wrapper,
    bridge_registry_to_fastmcp,
)
from gdx_dispatch.core.mcp_registry import list_tools
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor

# 35 tool modules on disk; sprint plan promised "the full toolset reaches
# claude.ai". The __init__.py side-effect import must register all 35.
EXPECTED_MIN_TOOLS = 35


def _fresh_mcp() -> FastMCP:
    """Per-test FastMCP — avoids cross-test name collisions."""
    return FastMCP(name="gdx-mcp-test")


def test_bridge_registers_every_legacy_tool() -> None:
    """Every name in `list_tools()` is exposed by FastMCP after the bridge runs."""
    mcp = _fresh_mcp()
    legacy_names = {d.name for d in list_tools()}
    assert len(legacy_names) >= EXPECTED_MIN_TOOLS, (
        f"only {len(legacy_names)} tools registered at module import; "
        f"expected >= {EXPECTED_MIN_TOOLS}. gdx_dispatch/core/mcp_tools/__init__.py "
        "must import every on-disk tool module."
    )

    # Bridge translates dotted internal names ("documents.list") to
    # underscore form ("documents_list") for FastMCP / claude.ai —
    # the connector's tool-name validator rejects dots.
    expected_external = {n.replace(".", "_") for n in legacy_names}
    registered = bridge_registry_to_fastmcp(mcp)
    assert set(registered) == expected_external

    fastmcp_tools = asyncio.run(mcp.list_tools())
    fastmcp_names = {t.name for t in fastmcp_tools}
    assert fastmcp_names == expected_external


def test_bridge_preserves_input_schema() -> None:
    """FastMCP exposes the same JSON-Schema parameters object the descriptor declares."""
    mcp = _fresh_mcp()
    bridge_registry_to_fastmcp(mcp)

    # FastMCP holds the external (underscore) name; map back to the
    # internal (dotted) descriptor by inverting the dot-to-underscore
    # rewrite.
    legacy_by_external = {d.name.replace(".", "_"): d for d in list_tools()}
    fastmcp_tools = asyncio.run(mcp.list_tools())
    assert fastmcp_tools, "expected at least one bridged tool"

    for ft in fastmcp_tools:
        descriptor = legacy_by_external[ft.name]
        expected = descriptor.input_schema or {"type": "object", "properties": {}}
        assert ft.parameters == expected, f"{ft.name}: schema mismatch"
        assert ft.description == descriptor.description


def test_bridge_wrapper_fails_loud_without_http_context() -> None:
    """A wrapper invocation outside an HTTP request must fail loudly.

    Pre-S4 this asserted ``PrincipalResolutionNotWired`` (S1 stub).
    After S4 the resolver reads ``request.state.mcp_claims`` via
    ``fastmcp.server.dependencies.get_http_request()``; calling the
    wrapper outside any FastMCP HTTP request must surface as
    ``PrincipalResolutionFailed`` rather than silently substitute an
    anonymous principal.
    """
    descriptor = next(iter(list_tools()))
    wrapper = _make_wrapper(descriptor)

    with pytest.raises(PrincipalResolutionFailed):
        asyncio.run(wrapper())


def test_bridge_fails_loud_on_empty_input_schema() -> None:
    """A descriptor with falsy input_schema must NOT silently get a default — raise."""
    from gdx_dispatch.core.mcp_registry import _DESCRIPTORS, _HANDLERS, register_tool

    probe_name = "bridge.bad_schema_probe"
    d = ToolDescriptor(
        name=probe_name,
        description="probe — empty schema",
        input_schema={},
        capabilities_required=[("read", "customer")],
    )

    async def _h(**_: object) -> dict[str, object]:
        return {}

    try:
        register_tool(d, _h)
        with pytest.raises(BridgeRegistrationError, match="empty/missing input_schema"):
            bridge_registry_to_fastmcp(_fresh_mcp())
    finally:
        _DESCRIPTORS.pop(probe_name, None)
        _HANDLERS.pop(probe_name, None)


def test_bridge_fails_loud_on_double_registration() -> None:
    """Running the bridge twice on the same FastMCP instance must raise — not warn."""
    mcp = _fresh_mcp()
    bridge_registry_to_fastmcp(mcp)
    with pytest.raises(BridgeRegistrationError, match="already registered"):
        bridge_registry_to_fastmcp(mcp)
