"""SS-29 slice E tests — verify CLI (sample-based drift detection)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import shadow_schema_map as ssm
from gdx_dispatch.core.shadow_schema_map import shadow_for
from gdx_dispatch.models.platform_ss29_additions import (
    SS29Base,
    ShadowMigrationDrift,
)
from gdx_dispatch.tools import shadow_migration_verify as v


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SS29Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    s = S()
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


def _build_store_from_source(source):
    """Pre-populate a correct new-table store."""
    sm = shadow_for("customers_v1")
    store = {sm.new_table: {}}
    for r in source:
        new_r = sm.transform_row(r)
        store[sm.new_table][new_r["id"]] = new_r
    return store


def test_verify_clean_no_drift(db):
    source = [
        {"id": i, "cust_name": f"n{i}", "cust_phone": f"+{i}"}
        for i in range(1, 11)
    ]
    store = _build_store_from_source(source)

    def read(nt, pk_col, pk):
        return store.get(nt, {}).get(pk)

    res = v.run_verify(
        db, tenant_id="t1", old_table="customers_v1",
        sample_old=v.sample_old_rows_from_list(source, seed=1),
        read_new_row=read, sample_size=5,
    )
    assert res.ok
    assert res.drift_count == 0
    assert res.sampled == 5
    assert db.query(ShadowMigrationDrift).count() == 0


def test_verify_detects_hash_mismatch(db):
    source = [
        {"id": i, "cust_name": f"n{i}", "cust_phone": f"+{i}"}
        for i in range(1, 6)
    ]
    store = _build_store_from_source(source)
    # Corrupt row 3 in the new-table store.
    store["customers_v2"][3]["customer_name"] = "CORRUPTED"

    def read(nt, pk_col, pk):
        return store.get(nt, {}).get(pk)

    res = v.run_verify(
        db, tenant_id="t1", old_table="customers_v1",
        sample_old=v.sample_old_rows_from_list(source, seed=0),
        read_new_row=read, sample_size=5,
    )
    assert not res.ok
    assert res.drift_count >= 1
    # Drift persisted.
    drifts = db.query(ShadowMigrationDrift).all()
    assert any(d.reason == "hash_mismatch" for d in drifts)


def test_verify_detects_missing_new_row(db):
    source = [
        {"id": i, "cust_name": f"n{i}", "cust_phone": f"+{i}"}
        for i in range(1, 4)
    ]

    def read(nt, pk_col, pk):
        return None  # nothing in new table

    res = v.run_verify(
        db, tenant_id="t1", old_table="customers_v1",
        sample_old=v.sample_old_rows_from_list(source, seed=0),
        read_new_row=read, sample_size=3,
    )
    assert not res.ok
    assert res.drift_count == 3
    assert all(d.reason == "new_row_missing" for d in res.drifts)


def test_verify_no_record_flag(db):
    source = [{"id": 1, "cust_name": "n", "cust_phone": "+"}]

    def read(nt, pk_col, pk):
        return None

    res = v.run_verify(
        db, tenant_id="t1", old_table="customers_v1",
        sample_old=v.sample_old_rows_from_list(source),
        read_new_row=read, sample_size=1, record_drift=False,
    )
    assert res.drift_count == 1
    # Not persisted.
    assert db.query(ShadowMigrationDrift).count() == 0
