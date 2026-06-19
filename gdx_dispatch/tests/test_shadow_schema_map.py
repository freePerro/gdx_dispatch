"""SS-29 slice A tests — shadow_schema_map loader + transforms."""
from __future__ import annotations

import json

import pytest

from gdx_dispatch.core import shadow_schema_map as ssm


def test_default_map_loads():
    m = ssm.reload_map()
    assert "customers_v1" in m
    entry = m["customers_v1"]
    assert entry.new_table == "customers_v2"
    assert entry.primary_key == "id"
    assert "cust_name" in entry.column_renames


def test_shadow_for_raises_on_unknown():
    ssm.reload_map()
    with pytest.raises(KeyError):
        ssm.shadow_for("no_such_table_zzz")


def test_is_shadowed():
    ssm.reload_map()
    assert ssm.is_shadowed("customers_v1")
    assert not ssm.is_shadowed("random_other_table")


def test_transform_row_renames_columns():
    ssm.reload_map()
    m = ssm.shadow_for("customers_v1")
    out = m.transform_row({"id": 1, "cust_name": "Acme", "cust_phone": "+1555"})
    assert out == {"id": 1, "customer_name": "Acme", "phone_e164": "+1555"}


def test_transform_row_does_not_mutate_input():
    ssm.reload_map()
    m = ssm.shadow_for("jobs_v1")
    src = {"id": 42, "job_no": "J-1"}
    m.transform_row(src)
    assert src == {"id": 42, "job_no": "J-1"}


def test_unknown_transform_raises(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "maps": {
            "t1": {
                "new_table": "t1_v2",
                "column_renames": {},
                "column_transformations": [
                    {"from": "x", "to": "x", "transform": "nonexistent"}
                ],
                "primary_key": "id",
            }
        }
    }))
    ssm.reload_map(bad)
    m = ssm.shadow_for("t1")
    with pytest.raises(ValueError, match="unknown transform"):
        m.transform_row({"x": "v"})


def test_transform_upper_lower(tmp_path):
    cfg = tmp_path / "m.json"
    cfg.write_text(json.dumps({
        "maps": {
            "t": {
                "new_table": "t_v2",
                "column_renames": {},
                "column_transformations": [
                    {"from": "a", "to": "a", "transform": "upper"},
                    {"from": "b", "to": "b", "transform": "lower"},
                ],
                "primary_key": "id",
            }
        }
    }))
    ssm.reload_map(cfg)
    out = ssm.shadow_for("t").transform_row({"a": "hi", "b": "LO"})
    assert out == {"a": "HI", "b": "lo"}


def test_transform_int_to_str_and_back(tmp_path):
    cfg = tmp_path / "m.json"
    cfg.write_text(json.dumps({
        "maps": {
            "t": {
                "new_table": "t_v2",
                "column_renames": {},
                "column_transformations": [
                    {"from": "n", "to": "n", "transform": "int_to_str"},
                ],
                "primary_key": "id",
            }
        }
    }))
    ssm.reload_map(cfg)
    out = ssm.shadow_for("t").transform_row({"n": 7})
    assert out == {"n": "7"}


def test_missing_new_table_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"maps": {"t1": {"primary_key": "id"}}}))
    with pytest.raises(ValueError, match="missing 'new_table'"):
        ssm.reload_map(bad)


def test_supported_transforms_nonempty():
    assert "identity" in ssm.supported_transforms()
    assert "upper" in ssm.supported_transforms()


def teardown_module():
    # Ensure following test modules get the real map, not a tmp one.
    ssm.reload_map()
