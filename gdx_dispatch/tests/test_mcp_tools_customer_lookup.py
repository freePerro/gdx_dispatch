"""Smoke tests for gdx_dispatch.core.mcp_tools.customer_lookup."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from gdx_dispatch.core.mcp_registry import CapabilityDenied, clear_registry
from gdx_dispatch.core.mcp_tools import customer_lookup as mod


@dataclass
class FakePrincipal:
    capabilities: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture(autouse=True)
def _reload_module():
    # The module registers on import. Other tests may have cleared the
    # registry, so re-register here for isolation.
    clear_registry()
    from gdx_dispatch.core.mcp_registry import register_tool
    register_tool(mod.DESCRIPTOR, mod.handler)
    yield
    clear_registry()


def test_descriptor_is_registered():
    assert mod.DESCRIPTOR.name == "customer.lookup"
    assert mod.DESCRIPTOR.capabilities_required == [("read", "customer")]


def test_handler_denies_without_capability():
    p = FakePrincipal()
    with pytest.raises(CapabilityDenied):
        asyncio.run(mod.handler(customer_id="abc", principal=p))


def test_handler_returns_stub_with_capability():
    p = FakePrincipal(capabilities=[{"action": "read", "resource_type": "customer"}])
    result = asyncio.run(mod.handler(customer_id="abc", principal=p))
    assert result["id"] == "abc"
    assert result.get("_stub") is True


def test_handler_rejects_empty_customer_id():
    p = FakePrincipal(capabilities=[{"action": "read", "resource_type": "customer"}])
    with pytest.raises(ValueError):
        asyncio.run(mod.handler(customer_id="", principal=p))
