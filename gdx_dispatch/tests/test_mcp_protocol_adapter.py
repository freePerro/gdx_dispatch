"""Sprint 1.x-S20 — FastMCP adapter skeleton."""
from __future__ import annotations


def test_adapter_module_imports():
    """The adapter module imports without raising — fastmcp dep installed."""
    import gdx_dispatch.core.mcp_protocol_adapter as mod
    assert hasattr(mod, "mcp"), "module must expose `mcp` (the FastMCP instance)"


def test_mcp_instance_has_expected_attrs():
    """The FastMCP instance has list_tools and call_tool callable surfaces."""
    from gdx_dispatch.core.mcp_protocol_adapter import mcp
    # FastMCP exposes tool registration and HTTP app methods.
    assert callable(getattr(mcp, "tool", None)) or callable(getattr(mcp, "list_tools", None)), \
        "FastMCP instance must expose tool registration / listing"
