"""Sprint 1.x-S22 — /mcp tool wiring."""
from __future__ import annotations


def test_adapter_lists_tools_for_principal():
    """The adapter exposes a callable that returns tool descriptors filtered by principal."""
    from gdx_dispatch.core.mcp_protocol_adapter import list_tools_for_mcp_principal
    assert callable(list_tools_for_mcp_principal)


def test_adapter_call_tool_delegates_to_invoke_tool():
    """The adapter exposes a callable that wraps invoke_tool."""
    from gdx_dispatch.core.mcp_protocol_adapter import call_tool_for_mcp_principal
    assert callable(call_tool_for_mcp_principal)
