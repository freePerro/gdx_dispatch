"""SS-33 Slice E tests: resource_instance_store helpers."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core import resource_instance_store as ris
from gdx_dispatch.core import resource_type_loader as loader
from gdx_dispatch.core import resource_type_registry as rtr


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Session = sessionmaker(bind=engine)
    s = Session()
    ris._create_table_for_tests(s)
    rtr._reset_for_tests()
    loader.load_builtin_types()
    yield s
    s.close()
    rtr._reset_for_tests()


def _veh_payload(**over):
    base = {"vin": "1HGBH41JXMN109186", "make": "Ford", "model": "Transit", "year": 2022}
    base.update(over)
    return base


def test_create_and_get(session):
    row = ris.create_instance(session, "gdx_dispatch.vehicle.v1", _veh_payload(), "acme")
    assert row["type_name"] == "gdx_dispatch.vehicle.v1"
    assert row["tenant_id"] == "acme"
    fetched = ris.get_instance(session, "gdx_dispatch.vehicle.v1", row["id"], "acme")
    assert fetched is not None
    assert fetched["payload"]["vin"] == "1HGBH41JXMN109186"


def test_create_validates_schema(session):
    with pytest.raises(rtr.ResourceSchemaError):
        ris.create_instance(
            session, "gdx_dispatch.vehicle.v1", {"vin": "X"}, "acme"  # missing make/model/year
        )


def test_create_rejects_unknown_type(session):
    with pytest.raises(rtr.ResourceTypeError):
        ris.create_instance(session, "gdx_dispatch.nope.v1", {}, "acme")


def test_create_blocks_foreign_tenant_on_private_type(session):
    rtr.register_type(
        "t_beta.foo.v1",
        {"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}},
        [],
        owner_tenant_id="beta",
    )
    with pytest.raises(ris.ResourceInstanceError, match="may not use"):
        ris.create_instance(session, "t_beta.foo.v1", {"x": "y"}, "acme")


def test_list_is_tenant_scoped(session):
    ris.create_instance(session, "gdx_dispatch.vehicle.v1", _veh_payload(vin="A"), "acme")
    ris.create_instance(session, "gdx_dispatch.vehicle.v1", _veh_payload(vin="B"), "beta")
    rows = ris.list_instances(session, "gdx_dispatch.vehicle.v1", "acme")
    assert len(rows) == 1
    assert rows[0]["payload"]["vin"] == "A"


def test_update_validates_schema(session):
    row = ris.create_instance(session, "gdx_dispatch.vehicle.v1", _veh_payload(), "acme")
    with pytest.raises(rtr.ResourceSchemaError):
        ris.update_instance(
            session, "gdx_dispatch.vehicle.v1", row["id"], {"vin": "X"}, "acme"
        )


def test_update_happy_path(session):
    row = ris.create_instance(session, "gdx_dispatch.vehicle.v1", _veh_payload(), "acme")
    updated = ris.update_instance(
        session,
        "gdx_dispatch.vehicle.v1",
        row["id"],
        _veh_payload(odometer_miles=5000),
        "acme",
    )
    assert updated is not None
    assert updated["payload"]["odometer_miles"] == 5000


def test_update_wrong_tenant_returns_none(session):
    row = ris.create_instance(session, "gdx_dispatch.vehicle.v1", _veh_payload(), "acme")
    result = ris.update_instance(
        session, "gdx_dispatch.vehicle.v1", row["id"], _veh_payload(), "beta"
    )
    assert result is None


def test_delete_soft_marks_deleted_at(session):
    row = ris.create_instance(session, "gdx_dispatch.vehicle.v1", _veh_payload(), "acme")
    assert ris.delete_instance(session, "gdx_dispatch.vehicle.v1", row["id"], "acme") is True
    assert ris.get_instance(session, "gdx_dispatch.vehicle.v1", row["id"], "acme") is None


def test_delete_nonexistent_returns_false(session):
    assert (
        ris.delete_instance(
            session, "gdx_dispatch.vehicle.v1", "00000000-0000-0000-0000-000000000000", "acme"
        )
        is False
    )


def test_platform_type_instances_isolated_per_tenant(session):
    """Platform-wide type is shared; instance data is tenant-private."""
    ris.create_instance(session, "gdx_dispatch.vehicle.v1", _veh_payload(vin="ACME"), "acme")
    ris.create_instance(session, "gdx_dispatch.vehicle.v1", _veh_payload(vin="BETA"), "beta")
    acme = ris.list_instances(session, "gdx_dispatch.vehicle.v1", "acme")
    beta = ris.list_instances(session, "gdx_dispatch.vehicle.v1", "beta")
    assert [r["payload"]["vin"] for r in acme] == ["ACME"]
    assert [r["payload"]["vin"] for r in beta] == ["BETA"]
