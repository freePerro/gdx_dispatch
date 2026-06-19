"""SS-29 slice D tests — backfill CLI (batched / idempotent / resumable)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import shadow_schema_map as ssm
from gdx_dispatch.models.platform_ss29_additions import (
    SS29Base,
    ShadowMigrationCheckpoint,
)
from gdx_dispatch.tools import shadow_migration_backfill as b


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


def _make_fetch(source_rows):
    """Build a fetch_batch that respects last_pk ordering on id."""
    def fetch(last_pk, limit):
        if last_pk is None:
            filtered = source_rows
        else:
            last_int = int(last_pk)
            filtered = [r for r in source_rows if r["id"] > last_int]
        filtered = sorted(filtered, key=lambda r: r["id"])
        return filtered[:limit]
    return fetch


def test_basic_backfill_copies_all_rows(db):
    source = [
        {"id": i, "cust_name": f"n{i}", "cust_phone": f"+{i}"}
        for i in range(1, 6)
    ]
    store: dict[str, dict] = {}

    def insert(new_table, row):
        store.setdefault(new_table, {})[row["id"]] = row

    res = b.run_backfill(
        db, tenant_id="t1", old_table="customers_v1",
        fetch_batch=_make_fetch(source), insert_row=insert, batch_size=2,
    )
    assert res.rows_processed == 5
    assert res.batches >= 3
    assert set(store["customers_v2"].keys()) == {1, 2, 3, 4, 5}
    assert store["customers_v2"][3]["customer_name"] == "n3"


def test_resume_from_checkpoint(db):
    source = [
        {"id": i, "cust_name": f"n{i}", "cust_phone": f"+{i}"}
        for i in range(1, 6)
    ]
    store: dict[str, dict] = {}

    def insert(new_table, row):
        store.setdefault(new_table, {})[row["id"]] = row

    # First run, only 2 rows.
    b.run_backfill(
        db, tenant_id="t1", old_table="customers_v1",
        fetch_batch=_make_fetch(source), insert_row=insert,
        batch_size=2, max_rows=2,
    )
    cp = db.query(ShadowMigrationCheckpoint).one()
    assert cp.last_row_pk == "2"

    # Second run should skip already-processed rows.
    res2 = b.run_backfill(
        db, tenant_id="t1", old_table="customers_v1",
        fetch_batch=_make_fetch(source), insert_row=insert, batch_size=2,
    )
    assert res2.resumed_from_pk == "2"
    # 3 remaining rows after checkpoint.
    assert res2.rows_processed == 3
    assert set(store["customers_v2"].keys()) == {1, 2, 3, 4, 5}


def test_idempotent_rerun_same_result(db):
    source = [
        {"id": i, "cust_name": f"n{i}", "cust_phone": f"+{i}"}
        for i in range(1, 4)
    ]
    store: dict[str, dict] = {}

    def insert(new_table, row):
        store.setdefault(new_table, {})[row["id"]] = row

    b.run_backfill(
        db, tenant_id="t1", old_table="customers_v1",
        fetch_batch=_make_fetch(source), insert_row=insert,
    )
    snapshot = dict(store["customers_v2"])

    # Rerun — already past checkpoint; no new inserts.
    res2 = b.run_backfill(
        db, tenant_id="t1", old_table="customers_v1",
        fetch_batch=_make_fetch(source), insert_row=insert,
    )
    assert res2.rows_processed == 0
    assert store["customers_v2"] == snapshot


def test_reset_checkpoint(db):
    source = [{"id": 1, "cust_name": "n", "cust_phone": "+1"}]
    store: dict[str, dict] = {}

    def insert(new_table, row):
        store.setdefault(new_table, {})[row["id"]] = row

    b.run_backfill(
        db, tenant_id="t1", old_table="customers_v1",
        fetch_batch=_make_fetch(source), insert_row=insert,
    )
    assert db.query(ShadowMigrationCheckpoint).count() == 1

    b.reset_checkpoint(db, "t1", "customers_v1")
    db.commit()
    assert db.query(ShadowMigrationCheckpoint).count() == 0


def test_error_preserves_checkpoint(db):
    source = [
        {"id": i, "cust_name": f"n{i}", "cust_phone": f"+{i}"}
        for i in range(1, 6)
    ]
    store: dict[str, dict] = {}

    def insert(new_table, row):
        if row["id"] == 3:
            raise RuntimeError("simulated")
        store.setdefault(new_table, {})[row["id"]] = row

    with pytest.raises(RuntimeError):
        b.run_backfill(
            db, tenant_id="t1", old_table="customers_v1",
            fetch_batch=_make_fetch(source), insert_row=insert, batch_size=2,
        )

    # Checkpoint should reflect the last successfully committed batch (id=2).
    cp = db.query(ShadowMigrationCheckpoint).one()
    assert cp.last_row_pk == "2"


def test_max_rows_limit(db):
    source = [
        {"id": i, "cust_name": f"n{i}", "cust_phone": f"+{i}"}
        for i in range(1, 20)
    ]
    inserted = []

    def insert(new_table, row):
        inserted.append(row["id"])

    res = b.run_backfill(
        db, tenant_id="t1", old_table="customers_v1",
        fetch_batch=_make_fetch(source), insert_row=insert,
        batch_size=5, max_rows=7,
    )
    assert res.rows_processed == 7
    assert inserted == [1, 2, 3, 4, 5, 6, 7]
