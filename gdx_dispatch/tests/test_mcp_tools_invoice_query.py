"""Smoke tests for gdx_dispatch.core.mcp_tools.invoice_query."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from gdx_dispatch.core.mcp_registry import CapabilityDenied, clear_registry, register_tool
from gdx_dispatch.core.mcp_tools import invoice_query as mod


@dataclass
class FakePrincipal:
    capabilities: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture(autouse=True)
def _iso():
    clear_registry()
    register_tool(mod.DESCRIPTOR, mod.handler)
    yield
    clear_registry()


def test_descriptor():
    assert mod.DESCRIPTOR.name == "invoice.query"
    assert mod.DESCRIPTOR.capabilities_required == [("read", "invoice")]


def test_denies_without_read_invoice():
    p = FakePrincipal(capabilities=[{"action": "read", "resource_type": "customer"}])
    with pytest.raises(CapabilityDenied):
        asyncio.run(mod.handler(principal=p))


def test_stub_returns_empty_list():
    p = FakePrincipal(capabilities=[{"action": "read", "resource_type": "invoice"}])
    r = asyncio.run(mod.handler(principal=p))
    assert r["invoices"] == []
    assert r["count"] == 0


def test_limit_bounds_enforced():
    p = FakePrincipal(capabilities=[{"action": "read", "resource_type": "invoice"}])
    with pytest.raises(ValueError):
        asyncio.run(mod.handler(limit=0, principal=p))
    with pytest.raises(ValueError):
        asyncio.run(mod.handler(limit=500, principal=p))
