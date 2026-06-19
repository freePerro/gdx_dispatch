"""SS-33 Slice B + C tests: filesystem + DB-backed type discovery."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gdx_dispatch.core import resource_type_loader as loader
from gdx_dispatch.core import resource_type_registry as rtr


@pytest.fixture(autouse=True)
def _clean():
    rtr._reset_for_tests()
    yield
    rtr._reset_for_tests()


def test_load_builtin_types_registers_all_three():
    names = loader.load_builtin_types()
    assert "gdx_dispatch.vehicle.v1" in names
    assert "gdx_dispatch.tool_inventory.v1" in names
    assert "gdx_dispatch.training_record.v1" in names

    v = rtr.get_type("gdx_dispatch.vehicle.v1")
    assert v is not None
    assert v["is_platform"] is True
    assert ("read", "vehicle") in v["capabilities"]
    assert "vin" in v["index_hints"]


def test_builtin_schemas_validate_correct_instances():
    loader.load_builtin_types()
    rtr.validate_instance(
        "gdx_dispatch.vehicle.v1",
        {"vin": "ABC123", "make": "Ford", "model": "Transit", "year": 2022},
    )
    rtr.validate_instance(
        "gdx_dispatch.tool_inventory.v1",
        {"sku": "DRL-001", "name": "Drill", "quantity": 5},
    )
    rtr.validate_instance(
        "gdx_dispatch.training_record.v1",
        {
            "technician_id": "tech-1",
            "course_name": "IICRC WRT",
            "completed_at": "2026-04-01",
        },
    )


def test_builtin_schemas_reject_bad_instances():
    loader.load_builtin_types()
    with pytest.raises(rtr.ResourceSchemaError):
        rtr.validate_instance("gdx_dispatch.vehicle.v1", {"vin": "X"})  # missing make/model/year
    with pytest.raises(rtr.ResourceSchemaError):
        rtr.validate_instance(
            "gdx_dispatch.tool_inventory.v1",
            {"sku": "X", "name": "Y", "quantity": -1},  # min=0
        )


def test_load_builtin_is_idempotent():
    a = loader.load_builtin_types()
    b = loader.load_builtin_types()
    assert a == b
    assert len(rtr.list_types(public_only=True)) == len(a)


def test_load_tenant_types_from_db_empty_session():
    session = MagicMock()
    session.execute.return_value = iter([])
    names = loader.load_tenant_types_from_db(session)
    assert names == []


def test_load_tenant_types_from_db_reads_rows():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    row = (
        "t_acme.thing.v1",
        json.dumps(schema),
        json.dumps([["read", "thing"]]),
        "acme",
        "Acme private type",
        json.dumps(["a"]),
    )
    session = MagicMock()
    session.execute.return_value = iter([row])
    names = loader.load_tenant_types_from_db(session)
    assert names == ["t_acme.thing.v1"]
    d = rtr.get_type("t_acme.thing.v1")
    assert d["owner_tenant_id"] == "acme"
    assert d["capabilities"] == [("read", "thing")]


def test_load_tenant_types_swallows_db_error():
    # The realistic "table not found" shape at pre-migration time is a
    # SQLAlchemy OperationalError, not a bare RuntimeError. The loader
    # narrows its catch so that genuinely unexpected errors (RuntimeError,
    # KeyboardInterrupt, etc.) now propagate — see resource_type_loader
    # comment above the try/except.
    from sqlalchemy.exc import OperationalError

    session = MagicMock()
    session.execute.side_effect = OperationalError(
        "SELECT ...", {}, Exception("no such table: resource_type")
    )
    assert loader.load_tenant_types_from_db(session) == []  # no raise


def test_bootstrap_runs_both_passes():
    session = MagicMock()
    session.execute.return_value = iter([])
    result = loader.bootstrap(session=session)
    assert "gdx_dispatch.vehicle.v1" in result["builtin"]
    assert result["tenant"] == []


def test_bootstrap_without_session_skips_db():
    result = loader.bootstrap(session=None)
    assert result["tenant"] == []
    assert len(result["builtin"]) >= 3
