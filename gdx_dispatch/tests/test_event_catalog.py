"""SS-23 Slice A: event catalog registry + validator tests."""
from __future__ import annotations

import pytest

from gdx_dispatch.core import event_catalog
from gdx_dispatch.core.event_catalog import (
    EventSchemaError,
    is_registered,
    list_event_types,
    register_schema,
    validate_event,
)


SAMPLE = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "gdx_dispatch.sample.created.v1",
    "type": "object",
    "required": ["id", "tenant_id"],
    "properties": {
        "id": {"type": "string"},
        "tenant_id": {"type": "string"},
        "count": {"type": "integer"},
    },
    "additionalProperties": False,
}


def test_register_and_list():
    register_schema("gdx_dispatch.sample.created.v1", SAMPLE)
    assert is_registered("gdx_dispatch.sample.created.v1")
    names = [e["event_type"] for e in list_event_types()]
    assert "gdx_dispatch.sample.created.v1" in names


def test_invalid_event_name_rejected():
    with pytest.raises(EventSchemaError):
        register_schema("bad.name", {})
    with pytest.raises(EventSchemaError):
        register_schema("gdx_dispatch.customer.created", {})  # missing v<N>
    with pytest.raises(EventSchemaError):
        register_schema("gdx_dispatch.Customer.created.v1", {})  # uppercase
    with pytest.raises(EventSchemaError):
        register_schema("gdx_dispatch.customer.created.vX", {})  # not numeric


def test_validate_unknown_type_raises():
    with pytest.raises(EventSchemaError, match="unknown event_type"):
        validate_event("gdx_dispatch.nope.created.v1", {})


def test_validate_happy_path():
    register_schema("gdx_dispatch.sample.created.v1", SAMPLE)
    validate_event("gdx_dispatch.sample.created.v1", {"id": "x", "tenant_id": "t"})


def test_validate_missing_required():
    register_schema("gdx_dispatch.sample.created.v1", SAMPLE)
    with pytest.raises(EventSchemaError):
        validate_event("gdx_dispatch.sample.created.v1", {"id": "x"})  # missing tenant_id


def test_validate_wrong_type():
    register_schema("gdx_dispatch.sample.created.v1", SAMPLE)
    with pytest.raises(EventSchemaError):
        validate_event(
            "gdx_dispatch.sample.created.v1",
            {"id": "x", "tenant_id": "t", "count": "not-an-int"},
        )


def test_validate_additional_properties_false():
    register_schema("gdx_dispatch.sample.created.v1", SAMPLE)
    with pytest.raises(EventSchemaError):
        validate_event(
            "gdx_dispatch.sample.created.v1",
            {"id": "x", "tenant_id": "t", "surprise": 1},
        )


def test_stdlib_validator_covers_subset():
    # Force stdlib path for coverage
    from gdx_dispatch.core.event_catalog import _stdlib_validate

    _stdlib_validate({"id": "x", "tenant_id": "t"}, SAMPLE)
    with pytest.raises(EventSchemaError):
        _stdlib_validate({"id": 1, "tenant_id": "t"}, SAMPLE)


def test_filesystem_discovery_loaded_builtin_schemas():
    # Slice B ships at least these 5 canonical event types.
    # Force a re-discover to recover from test-only resets.
    event_catalog._reset_for_tests()
    event_catalog._discover_schemas()
    names = {e["event_type"] for e in list_event_types()}
    expected = {
        "gdx_dispatch.customer.created.v1",
        "gdx_dispatch.job.completed.v1",
        "gdx_dispatch.pat.rotated.v1",
        "gdx_dispatch.invoice.paid.v1",
        "gdx_dispatch.custom_field_schema.changed.v1",
    }
    assert expected.issubset(names), f"missing: {expected - names}"


def test_bundled_schemas_validate_minimal_payloads():
    event_catalog._reset_for_tests()
    event_catalog._discover_schemas()
    validate_event(
        "gdx_dispatch.customer.created.v1",
        {"customer_id": "c1", "tenant_id": "t1", "created_at": "2026-04-19T00:00:00Z"},
    )
    validate_event(
        "gdx_dispatch.pat.rotated.v1",
        {"pat_id": "p1", "identity_id": "i1", "rotated_at": "2026-04-19T00:00:00Z"},
    )
    # pat.rotated has additionalProperties=false — prove it
    with pytest.raises(EventSchemaError):
        validate_event(
            "gdx_dispatch.pat.rotated.v1",
            {
                "pat_id": "p1",
                "identity_id": "i1",
                "rotated_at": "2026-04-19T00:00:00Z",
                "extra": "nope",
            },
        )
