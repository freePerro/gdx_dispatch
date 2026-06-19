"""Tests for gdx_dispatch.core.mcp_registry (SS-18 slice B)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from gdx_dispatch.core.mcp_registry import (
    CapabilityDenied,
    ToolAlreadyRegistered,
    check_capability,
    clear_registry,
    describe_tool,
    get_tool,
    list_tools,
    list_tools_for_principal,
    register_tool,
    require_capability,
)
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


@dataclass
class FakePrincipal:
    capabilities: list[dict[str, Any]] = field(default_factory=list)


async def _handler(**_kwargs):
    return {"ok": True}


async def _handler_alt(**_kwargs):
    return {"alt": True}


def _make_descriptor(**overrides) -> ToolDescriptor:
    kw = dict(
        name="customer.lookup",
        description="Look up a customer.",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        capabilities_required=[("read", "customer")],
        sensitivity_class="internal",
    )
    kw.update(overrides)
    return ToolDescriptor(**kw)


@pytest.fixture(autouse=True)
def _reset_registry():
    clear_registry()
    yield
    clear_registry()


def test_register_and_list():
    d = _make_descriptor()
    register_tool(d, _handler)
    assert list_tools() == [d]


def test_register_sorted_by_name():
    a = _make_descriptor(name="a.one")
    b = _make_descriptor(name="b.two")
    register_tool(b, _handler)
    register_tool(a, _handler)
    assert [t.name for t in list_tools()] == ["a.one", "b.two"]


def test_get_tool_and_describe_tool():
    d = _make_descriptor()
    register_tool(d, _handler)
    got = get_tool("customer.lookup")
    assert got is not None
    assert got[0] is d
    assert got[1] is _handler
    assert describe_tool("customer.lookup") is d
    assert describe_tool("missing") is None
    assert get_tool("missing") is None


def test_register_idempotent_same_args():
    d = _make_descriptor()
    register_tool(d, _handler)
    register_tool(d, _handler)  # no-op
    assert len(list_tools()) == 1


def test_register_conflict_different_handler():
    d = _make_descriptor()
    register_tool(d, _handler)
    with pytest.raises(ToolAlreadyRegistered):
        register_tool(d, _handler_alt)


def test_register_conflict_different_descriptor():
    d1 = _make_descriptor(description="one")
    d2 = _make_descriptor(description="two")
    register_tool(d1, _handler)
    with pytest.raises(ToolAlreadyRegistered):
        register_tool(d2, _handler)


def test_register_rejects_non_descriptor():
    with pytest.raises(TypeError):
        register_tool({"name": "x"}, _handler)  # type: ignore[arg-type]


def test_register_rejects_non_callable_handler():
    with pytest.raises(TypeError):
        register_tool(_make_descriptor(), "not-callable")  # type: ignore[arg-type]


# ── Capability gating ─────────────────────────────────────────────────────


def test_check_capability_exact_match():
    d = _make_descriptor()
    p = FakePrincipal(capabilities=[{"action": "read", "resource_type": "customer"}])
    assert check_capability(p, d) is True


def test_check_capability_wildcard():
    d = _make_descriptor()
    p = FakePrincipal(capabilities=[{"action": "*", "resource_type": "*"}])
    assert check_capability(p, d) is True


def test_check_capability_missing():
    d = _make_descriptor()
    p = FakePrincipal(capabilities=[{"action": "read", "resource_type": "job"}])
    assert check_capability(p, d) is False


def test_check_capability_empty_principal():
    d = _make_descriptor()
    assert check_capability(FakePrincipal(), d) is False


def test_restricted_tool_requires_restricted_capability():
    d = _make_descriptor(sensitivity_class="restricted")
    # Non-restricted capability does NOT suffice.
    p_plain = FakePrincipal(capabilities=[{"action": "read", "resource_type": "customer"}])
    assert check_capability(p_plain, d) is False
    # Restricted capability does.
    p_restricted = FakePrincipal(
        capabilities=[{"action": "read", "resource_type": "customer", "restricted": True}]
    )
    assert check_capability(p_restricted, d) is True


def test_restricted_tool_wildcard_requires_restricted_flag():
    d = _make_descriptor(sensitivity_class="restricted")
    p_wild = FakePrincipal(capabilities=[{"action": "*", "resource_type": "*"}])
    assert check_capability(p_wild, d) is False  # wildcard not restricted
    p_wild_r = FakePrincipal(capabilities=[{"action": "*", "resource_type": "*", "restricted": True}])
    assert check_capability(p_wild_r, d) is True


def test_multiple_required_caps_all_needed():
    d = _make_descriptor(capabilities_required=[("read", "customer"), ("read", "job")])
    p = FakePrincipal(capabilities=[{"action": "read", "resource_type": "customer"}])
    assert check_capability(p, d) is False
    p.capabilities.append({"action": "read", "resource_type": "job"})
    assert check_capability(p, d) is True


def test_list_tools_for_principal_filters():
    a = _make_descriptor(name="a.read", capabilities_required=[("read", "customer")])
    b = _make_descriptor(name="b.admin", capabilities_required=[("admin", "tenant")])
    register_tool(a, _handler)
    register_tool(b, _handler)
    p = FakePrincipal(capabilities=[{"action": "read", "resource_type": "customer"}])
    visible = [t.name for t in list_tools_for_principal(p)]
    assert visible == ["a.read"]


def test_require_capability_raises_on_denial():
    d = _make_descriptor()
    p = FakePrincipal()
    with pytest.raises(CapabilityDenied) as exc:
        require_capability(p, d)
    assert "customer.lookup" in str(exc.value)


def test_require_capability_passes():
    d = _make_descriptor()
    p = FakePrincipal(capabilities=[{"action": "read", "resource_type": "customer"}])
    require_capability(p, d)  # no raise


def test_check_capability_accepts_tuple_shaped_caps():
    d = _make_descriptor()
    p = FakePrincipal(capabilities=[("read", "customer")])  # type: ignore[list-item]
    assert check_capability(p, d) is True


# ── Pagination (red-team Pattern 6) ───────────────────────────────────────


def test_list_tools_offset_and_limit():
    for i in range(5):
        register_tool(_make_descriptor(name=f"t.{i:02d}"), _handler)
    all_names = [t.name for t in list_tools()]
    assert all_names == ["t.00", "t.01", "t.02", "t.03", "t.04"]

    # limit
    assert [t.name for t in list_tools(limit=2)] == ["t.00", "t.01"]
    # offset
    assert [t.name for t in list_tools(offset=3)] == ["t.03", "t.04"]
    # offset + limit
    assert [t.name for t in list_tools(offset=1, limit=2)] == ["t.01", "t.02"]
    # offset beyond end → empty
    assert list_tools(offset=100) == []


def test_list_tools_for_principal_paginates():
    for i in range(4):
        register_tool(
            _make_descriptor(name=f"t.{i:02d}", capabilities_required=[("read", "customer")]),
            _handler,
        )
    p = FakePrincipal(capabilities=[{"action": "read", "resource_type": "customer"}])
    assert [t.name for t in list_tools_for_principal(p, limit=2)] == ["t.00", "t.01"]
    assert [t.name for t in list_tools_for_principal(p, offset=2, limit=5)] == [
        "t.02",
        "t.03",
    ]


def test_list_tools_rejects_negative():
    with pytest.raises(ValueError):
        list_tools(offset=-1)
    with pytest.raises(ValueError):
        list_tools(limit=-5)
