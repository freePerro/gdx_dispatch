"""Smoke tests for gdx_dispatch.core.mcp_tools.event_emit."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from gdx_dispatch.core.mcp_registry import CapabilityDenied, clear_registry, register_tool
from gdx_dispatch.core.mcp_tools import event_emit as mod


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
    assert mod.DESCRIPTOR.name == "event.emit"
    assert mod.DESCRIPTOR.capabilities_required == [("emit", "event")]


def test_denies_without_emit_event():
    p = FakePrincipal()
    with pytest.raises(CapabilityDenied):
        asyncio.run(mod.handler(event_name="x.created", payload={}, principal=p))


def test_rejects_non_dict_payload():
    p = FakePrincipal(capabilities=[{"action": "emit", "resource_type": "event"}])
    with pytest.raises(ValueError):
        asyncio.run(mod.handler(event_name="x.created", payload="not-a-dict", principal=p))  # type: ignore[arg-type]


def test_rejects_empty_event_name():
    p = FakePrincipal(capabilities=[{"action": "emit", "resource_type": "event"}])
    with pytest.raises(ValueError):
        asyncio.run(mod.handler(event_name="", payload={}, principal=p))


def test_happy_path_returns_emitted_flag():
    p = FakePrincipal(capabilities=[{"action": "emit", "resource_type": "event"}])
    r = asyncio.run(mod.handler(event_name="customer.created", payload={"id": "c1"}, principal=p))
    # stub or real — both shapes have 'emitted' key
    assert "emitted" in r
    assert "event_id" in r
