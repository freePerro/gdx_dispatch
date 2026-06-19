"""SS-29 slice B tests — ShadowWriter dual-write behavior."""
from __future__ import annotations

import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import shadow_migration as sm
from gdx_dispatch.core import shadow_schema_map as ssm
from gdx_dispatch.models.platform_ss29_additions import (
    SS29Base,
    ShadowMigrationDrift,
    ShadowMigrationState,
)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SS29Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def reset_map():
    ssm.reload_map()
    yield
    ssm.reload_map()


def _set_mode(db, tenant_id, old_table, mode):
    row = ShadowMigrationState(
        tenant_id=tenant_id, old_table=old_table,
        new_table="customers_v2", mode=mode,
    )
    db.add(row)
    db.flush()


def test_off_mode_is_noop(db):
    w = sm.ShadowWriter(db, mode_lookup=lambda t, tbl: sm.MODE_OFF)
    res = w.shadow_write(
        tenant_id="t1", old_table="customers_v1",
        old_row={"id": 1, "cust_name": "Acme", "cust_phone": "+1"},
    )
    assert res.mode == sm.MODE_OFF
    assert res.shadowed is False
    assert res.drift is False
    assert db.query(ShadowMigrationDrift).count() == 0


def test_shadow_mode_inserts_and_no_drift(db):
    store = {}  # new_table -> {pk: row}

    def fake_insert(new_table, row):
        store.setdefault(new_table, {})[row["id"]] = row

    def fake_read(new_table, pk_col, pk_value):
        return store.get(new_table, {}).get(pk_value)

    w = sm.ShadowWriter(
        db,
        mode_lookup=lambda t, tbl: sm.MODE_SHADOW,
        insert_new_row=fake_insert,
        read_new_row=fake_read,
    )
    res = w.shadow_write(
        tenant_id="t1", old_table="customers_v1",
        old_row={"id": 1, "cust_name": "Acme", "cust_phone": "+1"},
    )
    assert res.shadowed is True
    assert res.drift is False
    # The inserted row uses renamed columns.
    assert "customers_v2" in store
    assert store["customers_v2"][1]["customer_name"] == "Acme"
    assert db.query(ShadowMigrationDrift).count() == 0


def test_hash_mismatch_records_drift(db):
    store = {"customers_v2": {}}

    def fake_insert(new_table, row):
        # Corrupt the row on write.
        store[new_table][row["id"]] = {**row, "customer_name": "corrupted"}

    def fake_read(new_table, pk_col, pk_value):
        return store.get(new_table, {}).get(pk_value)

    w = sm.ShadowWriter(
        db,
        mode_lookup=lambda t, tbl: sm.MODE_SHADOW,
        insert_new_row=fake_insert,
        read_new_row=fake_read,
    )
    res = w.shadow_write(
        tenant_id="t1", old_table="customers_v1",
        old_row={"id": 1, "cust_name": "Acme", "cust_phone": "+1"},
    )
    assert res.shadowed is True
    assert res.drift is True
    assert res.drift_reason == "hash_mismatch"
    drifts = db.query(ShadowMigrationDrift).all()
    assert len(drifts) == 1
    assert drifts[0].reason == "hash_mismatch"
    assert drifts[0].old_hash != drifts[0].new_hash


def test_insert_failure_never_raises(db, caplog):
    def boom(new_table, row):
        raise RuntimeError("db down")

    w = sm.ShadowWriter(
        db,
        mode_lookup=lambda t, tbl: sm.MODE_SHADOW,
        insert_new_row=boom,
    )
    caplog.set_level(logging.ERROR)
    res = w.shadow_write(
        tenant_id="t1", old_table="customers_v1",
        old_row={"id": 1, "cust_name": "A", "cust_phone": "+1"},
    )
    assert res.shadowed is False
    assert res.drift is True
    assert res.drift_reason == "insert_failed"
    # Structured log: look for the event key and structured error_type field.
    insert_failed_records = [r for r in caplog.records if "insert_failed" in r.message]
    assert insert_failed_records, "expected shadow_writer.insert_failed error log"
    assert any(
        getattr(r, "error_type", None) == "RuntimeError"
        for r in insert_failed_records
    ), "expected error_type=RuntimeError in structured log extra"
    assert db.query(ShadowMigrationDrift).count() == 1


def test_new_row_missing_after_insert(db):
    def fake_insert(new_table, row):
        pass  # pretend it worked but don't store

    def fake_read(new_table, pk_col, pk_value):
        return None

    w = sm.ShadowWriter(
        db,
        mode_lookup=lambda t, tbl: sm.MODE_SHADOW,
        insert_new_row=fake_insert,
        read_new_row=fake_read,
    )
    res = w.shadow_write(
        tenant_id="t1", old_table="customers_v1",
        old_row={"id": 1, "cust_name": "A", "cust_phone": "+1"},
    )
    assert res.drift is True
    assert res.drift_reason == "new_row_missing"


def test_row_fingerprint_stable():
    a = {"x": 1, "y": "hi"}
    b = {"y": "hi", "x": 1}
    assert sm.row_fingerprint(a) == sm.row_fingerprint(b)


def test_unknown_table_is_off(db):
    w = sm.ShadowWriter(db, mode_lookup=lambda t, tbl: sm.MODE_SHADOW)
    res = w.shadow_write(
        tenant_id="t1", old_table="not_in_map",
        old_row={"id": 1},
    )
    # Not in map → treated as off-equivalent (not shadowed, no drift).
    assert res.shadowed is False
    assert res.drift is False


def test_mode_lookup_from_db(db):
    _set_mode(db, "t1", "customers_v1", sm.MODE_SHADOW)
    db.commit()
    # Use default _db_mode_lookup.
    w = sm.ShadowWriter(db)
    assert w.current_mode("t1", "customers_v1") == sm.MODE_SHADOW
    assert w.current_mode("t1", "jobs_v1") == sm.MODE_OFF


def test_required_args(db):
    w = sm.ShadowWriter(db, mode_lookup=lambda t, tbl: sm.MODE_OFF)
    with pytest.raises(ValueError):
        w.shadow_write(tenant_id="", old_table="customers_v1", old_row={})
    with pytest.raises(ValueError):
        w.shadow_write(tenant_id="t", old_table="", old_row={})
