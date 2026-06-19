"""Tests for gdx_dispatch.core.mcp_tool_descriptor (SS-18 slice A)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor


def _base_kwargs(**overrides):
    kw = dict(
        name="customer.lookup",
        description="Look up a customer by id.",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
        output_schema={"type": "object"},
        capabilities_required=[("read", "customer")],
        sensitivity_class="internal",
    )
    kw.update(overrides)
    return kw


def test_descriptor_happy_path():
    d = ToolDescriptor(**_base_kwargs())
    assert d.name == "customer.lookup"
    assert d.capabilities_required == [("read", "customer")]
    assert d.sensitivity_class == "internal"
    assert d.approval_required is False


def test_descriptor_is_frozen():
    d = ToolDescriptor(**_base_kwargs())
    with pytest.raises(ValidationError):
        d.name = "other.name"  # type: ignore[misc]


def test_capabilities_reject_colon_strings():
    with pytest.raises(ValidationError) as exc:
        ToolDescriptor(**_base_kwargs(capabilities_required=["read:customer"]))
    assert "colon-string" in str(exc.value)


def test_capabilities_reject_wrong_arity():
    with pytest.raises(ValidationError):
        ToolDescriptor(**_base_kwargs(capabilities_required=[("read", "customer", "extra")]))


def test_capabilities_reject_empty_parts():
    with pytest.raises(ValidationError):
        ToolDescriptor(**_base_kwargs(capabilities_required=[("", "customer")]))
    with pytest.raises(ValidationError):
        ToolDescriptor(**_base_kwargs(capabilities_required=[("read", "")]))


def test_name_must_not_be_empty():
    with pytest.raises(ValidationError):
        ToolDescriptor(**_base_kwargs(name=""))


def test_name_disallows_spaces():
    with pytest.raises(ValidationError):
        ToolDescriptor(**_base_kwargs(name="customer lookup"))


def test_sensitivity_class_enum():
    for sc in ("public", "internal", "restricted"):
        d = ToolDescriptor(**_base_kwargs(sensitivity_class=sc))
        assert d.sensitivity_class == sc
    with pytest.raises(ValidationError):
        ToolDescriptor(**_base_kwargs(sensitivity_class="secret"))


def test_schema_shape_rejects_bare_dict():
    with pytest.raises(ValidationError):
        ToolDescriptor(**_base_kwargs(input_schema={"properties": {}}))


def test_schema_allows_empty_dict():
    d = ToolDescriptor(**_base_kwargs(input_schema={}, output_schema={}))
    assert d.input_schema == {}


def test_to_public_dict_serialises_tuples_as_lists():
    d = ToolDescriptor(**_base_kwargs(capabilities_required=[("read", "customer"), ("read", "job")]))
    pub = d.to_public_dict()
    assert pub["capabilities_required"] == [["read", "customer"], ["read", "job"]]


def test_approval_required_default_false():
    d = ToolDescriptor(**_base_kwargs())
    assert d.approval_required is False
    d2 = ToolDescriptor(**_base_kwargs(approval_required=True))
    assert d2.approval_required is True
