"""Smoke tests for gdx_dispatch.core.mcp_tools.job_create."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from gdx_dispatch.core.mcp_registry import CapabilityDenied, clear_registry, register_tool
from gdx_dispatch.core.mcp_tools import job_create as mod


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
    assert mod.DESCRIPTOR.name == "job.create"
    assert mod.DESCRIPTOR.capabilities_required == [("write", "job")]
    assert mod.DESCRIPTOR.sensitivity_class == "internal"


def test_denies_without_write_job():
    p = FakePrincipal(capabilities=[{"action": "read", "resource_type": "job"}])
    with pytest.raises(CapabilityDenied):
        asyncio.run(mod.handler(customer_id="c1", description="d", principal=p))


def test_stub_returns_scheduled():
    p = FakePrincipal(capabilities=[{"action": "write", "resource_type": "job"}])
    r = asyncio.run(mod.handler(customer_id="c1", description="broken spring", principal=p))
    assert r["status"] == "scheduled"


def test_missing_description_raises():
    p = FakePrincipal(capabilities=[{"action": "write", "resource_type": "job"}])
    with pytest.raises(ValueError):
        asyncio.run(mod.handler(customer_id="c1", description="", principal=p))
