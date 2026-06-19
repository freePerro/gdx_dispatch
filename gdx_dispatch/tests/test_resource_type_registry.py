"""SS-33 Slice A tests: resource_type_registry."""
from __future__ import annotations

import pytest

from gdx_dispatch.core import resource_type_registry as rtr
from gdx_dispatch.core.resource_type_registry import (
    ResourceSchemaError,
    ResourceTypeError,
    _reset_for_tests,
    get_type,
    list_types,
    register_type,
    unregister_type,
    validate_instance,
)


SAMPLE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["vin"],
    "properties": {
        "vin": {"type": "string"},
        "miles": {"type": "integer"},
    },
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _clean():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_register_platform_type_ok():
    d = register_type("gdx_dispatch.vehicle.v1", SAMPLE_SCHEMA, [("read", "vehicle")])
    assert d["name"] == "gdx_dispatch.vehicle.v1"
    assert d["is_platform"] is True
    assert d["owner_tenant_id"] is None
    assert d["capabilities"] == [("read", "vehicle")]
    assert get_type("gdx_dispatch.vehicle.v1") is d


def test_register_tenant_private_type_ok():
    d = register_type(
        "t_acme.thing.v1",
        SAMPLE_SCHEMA,
        [],
        owner_tenant_id="acme",
    )
    assert d["is_platform"] is False
    assert d["owner_tenant_id"] == "acme"


def test_platform_name_must_match_gdx_prefix():
    with pytest.raises(ResourceTypeError, match="gdx_dispatch."):
        register_type("acme.thing.v1", SAMPLE_SCHEMA, [])


def test_tenant_name_must_use_t_prefix():
    with pytest.raises(ResourceTypeError, match="t_"):
        register_type("gdx_dispatch.thing.v1", SAMPLE_SCHEMA, [], owner_tenant_id="acme")


def test_name_must_have_version():
    with pytest.raises(ResourceTypeError):
        register_type("gdx_dispatch.thing", SAMPLE_SCHEMA, [])


def test_colon_string_caps_banned():
    with pytest.raises(ResourceTypeError, match="colon-strings"):
        register_type("gdx_dispatch.thing.v1", SAMPLE_SCHEMA, ["read:thing"])


def test_caps_must_be_pair():
    with pytest.raises(ResourceTypeError):
        register_type("gdx_dispatch.thing.v1", SAMPLE_SCHEMA, [("read",)])


def test_schema_must_have_top_level_type():
    with pytest.raises(ResourceTypeError, match="top level"):
        register_type("gdx_dispatch.thing.v1", {"properties": {}}, [])


def test_reregister_same_descriptor_is_idempotent():
    a = register_type("gdx_dispatch.thing.v1", SAMPLE_SCHEMA, [("read", "thing")])
    b = register_type("gdx_dispatch.thing.v1", SAMPLE_SCHEMA, [("read", "thing")])
    assert a is b


def test_reregister_different_descriptor_raises():
    register_type("gdx_dispatch.thing.v1", SAMPLE_SCHEMA, [("read", "thing")])
    with pytest.raises(ResourceTypeError, match="already registered"):
        register_type("gdx_dispatch.thing.v1", SAMPLE_SCHEMA, [("write", "thing")])


def test_list_types_public_only():
    register_type("gdx_dispatch.thing.v1", SAMPLE_SCHEMA, [])
    register_type("t_acme.priv.v1", SAMPLE_SCHEMA, [], owner_tenant_id="acme")
    names = [d["name"] for d in list_types(public_only=True)]
    assert names == ["gdx_dispatch.thing.v1"]


def test_list_types_scoped_to_tenant():
    register_type("gdx_dispatch.thing.v1", SAMPLE_SCHEMA, [])
    register_type("t_acme.priv.v1", SAMPLE_SCHEMA, [], owner_tenant_id="acme")
    register_type("t_beta.priv.v1", SAMPLE_SCHEMA, [], owner_tenant_id="beta")
    names = {d["name"] for d in list_types(owner_tenant_id="acme")}
    assert names == {"gdx_dispatch.thing.v1", "t_acme.priv.v1"}


def test_validate_instance_ok():
    register_type("gdx_dispatch.vehicle.v1", SAMPLE_SCHEMA, [])
    validate_instance("gdx_dispatch.vehicle.v1", {"vin": "ABC", "miles": 10})


def test_validate_instance_missing_required():
    register_type("gdx_dispatch.vehicle.v1", SAMPLE_SCHEMA, [])
    with pytest.raises(ResourceSchemaError) as ei:
        validate_instance("gdx_dispatch.vehicle.v1", {"miles": 10})
    assert ei.value.code == "schema_violation"


def test_validate_instance_extra_property_rejected_on_closed_schema():
    register_type("gdx_dispatch.vehicle.v1", SAMPLE_SCHEMA, [])
    with pytest.raises(ResourceSchemaError):
        validate_instance("gdx_dispatch.vehicle.v1", {"vin": "X", "nope": 1})


def test_validate_instance_unknown_type():
    with pytest.raises(ResourceTypeError, match="unknown"):
        validate_instance("gdx_dispatch.nope.v1", {})


def test_unregister_platform_requires_super_admin():
    register_type("gdx_dispatch.thing.v1", SAMPLE_SCHEMA, [])
    with pytest.raises(ResourceTypeError, match="super-admin"):
        unregister_type("gdx_dispatch.thing.v1")
    unregister_type("gdx_dispatch.thing.v1", super_admin=True)
    assert get_type("gdx_dispatch.thing.v1") is None


def test_unregister_tenant_type_ok_without_super_admin():
    register_type("t_acme.priv.v1", SAMPLE_SCHEMA, [], owner_tenant_id="acme")
    unregister_type("t_acme.priv.v1")
    assert get_type("t_acme.priv.v1") is None


def test_unregister_unknown_raises():
    with pytest.raises(ResourceTypeError, match="unknown"):
        unregister_type("gdx_dispatch.nope.v1")
