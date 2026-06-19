"""Smoke tests for gdx_dispatch.core.mcp_tools.pat_rotate."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from gdx_dispatch.core.mcp_registry import CapabilityDenied, clear_registry, register_tool
from gdx_dispatch.core.mcp_tools import pat_rotate as mod


@dataclass
class FakePrincipal:
    capabilities: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture(autouse=True)
def _iso():
    clear_registry()
    register_tool(mod.DESCRIPTOR, mod.handler)
    yield
    clear_registry()


def test_descriptor_is_restricted_and_approval_required():
    assert mod.DESCRIPTOR.name == "pat.rotate"
    assert mod.DESCRIPTOR.sensitivity_class == "restricted"
    assert mod.DESCRIPTOR.approval_required is True
    assert mod.DESCRIPTOR.capabilities_required == [("admin", "pat")]


def test_plain_admin_pat_capability_is_not_enough():
    # sensitivity_class='restricted' demands restricted=True.
    p = FakePrincipal(capabilities=[{"action": "admin", "resource_type": "pat"}])
    with pytest.raises(CapabilityDenied):
        asyncio.run(mod.handler(pat_id="pat-123", principal=p))


def test_restricted_cap_returns_pending_approval():
    p = FakePrincipal(
        capabilities=[{"action": "admin", "resource_type": "pat", "restricted": True}]
    )
    r = asyncio.run(mod.handler(pat_id="pat-123", reason="leak", principal=p))
    assert r["status"] == "pending_approval"
    assert r["requires_approval"] is True
    assert r["pat_id"] == "pat-123"
    assert r["reason"] == "leak"
    assert r["rotation_id"]  # uuid


def test_missing_pat_id_raises():
    p = FakePrincipal(
        capabilities=[{"action": "admin", "resource_type": "pat", "restricted": True}]
    )
    with pytest.raises(ValueError):
        asyncio.run(mod.handler(pat_id="", principal=p))
