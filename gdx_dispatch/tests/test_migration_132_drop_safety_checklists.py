"""Migration 132: drop ``safety_checklists`` (GDXA-1714, retired outright).

On both engines, reversibly, rerunnably, and only when the table is empty: a
row refuses the migration and keeps the data. The router left in GDXA-1833,
the performance stat in GDXA-1832, the model in this change.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
import types

import pytest
from sqlalchemy import create_engine, inspect

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/132_drop_safety_checklists.py"
TABLE = "safety_checklists"
COLUMNS = {
    "id", "company_id", "job_id", "technician_id", "items", "completed",
    "photo_url", "signed_at", "created_at", "deleted_at",
}

SEED = [
    "CREATE TABLE tenants (id VARCHAR(36) PRIMARY KEY, slug VARCHAR(100))",
    "INSERT INTO tenants (id, slug) VALUES ('t1', 'gdx')",
    # The shape create_all made on prod (structure.sql before this change).
    "CREATE TABLE safety_checklists (id VARCHAR(36) PRIMARY KEY, company_id VARCHAR(36) NOT NULL, "
    "job_id VARCHAR(36) NOT NULL, technician_id VARCHAR(36) NOT NULL, items TEXT NOT NULL, completed BOOLEAN, "
    "photo_url TEXT, signed_at TIMESTAMP, created_at TIMESTAMP, deleted_at TIMESTAMP)",
]
OCCUPY = (
    "INSERT INTO safety_checklists (id, company_id, job_id, technician_id, items) "
    "VALUES ('s1', 't1', 'j1', 'u1', '[]')"
)


def _load(conn):
    """Import the migration with `op.get_bind()` pointed at our connection."""
    spec = importlib.util.spec_from_file_location("m132", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    fake = types.ModuleType("alembic")

    class _Op:
        def get_bind(self):
            return conn

    fake.op = _Op()
    saved = sys.modules.get("alembic")
    sys.modules["alembic"] = fake
    try:
        spec.loader.exec_module(mod)
    finally:
        if saved is not None:
            sys.modules["alembic"] = saved
        else:
            sys.modules.pop("alembic", None)
    return mod


def _tables(conn) -> set[str]:
    return set(inspect(conn).get_table_names())


def _rows(conn) -> int:
    return conn.exec_driver_sql(f"SELECT COUNT(*) FROM {TABLE}").scalar()  # noqa: S608 — test constant


# ── Static ───────────────────────────────────────────────────────────────────

def test_it_chains_onto_131_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "132_drop_safety_checklists"' in source
    assert 'down_revision = "131_vacation_year_policy"' in source
    assert len("132_drop_safety_checklists") <= 32, "alembic_version.version_num is varchar(32)"
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "131_vacation_year_policy"', p.read_text(), re.M)
    ]
    assert revising == ["132_drop_safety_checklists.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_no_model_declares_the_dropped_table() -> None:
    """create_all runs at every boot: the ORM must not re-create what this drops."""
    import gdx_dispatch.models as models
    from gdx_dispatch.core.audit import TenantBase

    assert TABLE not in TenantBase.metadata.tables
    assert not hasattr(models, "SafetyChecklist")


# ── SQLite ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm132.db'}", future=True)
    with eng.begin() as c:
        for stmt in SEED:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_drops_the_empty_table_and_nothing_else(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    assert _tables(sqlite_conn) == {"tenants"}
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM tenants").scalar() == 1


def test_sqlite_an_occupied_table_refuses_and_keeps_its_row(sqlite_conn):
    sqlite_conn.exec_driver_sql(OCCUPY)
    m = _load(sqlite_conn)
    with pytest.raises(RuntimeError, match=r"safety_checklists holds 1 row"):
        m.upgrade()
    assert TABLE in _tables(sqlite_conn)
    assert _rows(sqlite_conn) == 1


def test_sqlite_a_soft_deleted_row_still_refuses(sqlite_conn):
    """deleted_at is not consent to lose the row: the count is of every row."""
    sqlite_conn.exec_driver_sql(OCCUPY)
    sqlite_conn.exec_driver_sql(f"UPDATE {TABLE} SET deleted_at = '2026-10-19 00:00:00'")  # noqa: S608 — test constant
    with pytest.raises(RuntimeError, match=r"holds 1 row"):
        _load(sqlite_conn).upgrade()


def test_sqlite_upgrade_is_rerunnable_and_a_fresh_install_is_a_no_op(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()
    assert _tables(sqlite_conn) == {"tenants"}


def test_sqlite_downgrade_recreates_the_model_shape_empty(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    assert {c["name"] for c in inspect(sqlite_conn).get_columns(TABLE)} == COLUMNS
    assert _rows(sqlite_conn) == 0
    m.downgrade()  # rerun must not abort either
    m.upgrade()    # and the round trip closes
    assert _tables(sqlite_conn) == {"tenants"}


# ── Postgres (fixtures/pg.py; fails rather than skips under CI) ─────────────

@pytest.fixture()
def pg_conn(pg_test_db):
    eng = create_engine(pg_test_db, future=True)
    with eng.begin() as c:
        # The template is the ORM, which no longer has the table; tenants is
        # already there, so seed only the dropped table.
        assert TABLE not in _tables(c)
        c.exec_driver_sql(SEED[2])
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_pg_upgrade_then_downgrade_round_trip(pg_conn):
    m = _load(pg_conn)
    before = _tables(pg_conn)
    m.upgrade()
    assert _tables(pg_conn) == before - {TABLE}
    m.upgrade()  # rerunnable
    m.downgrade()
    assert {c["name"] for c in inspect(pg_conn).get_columns(TABLE)} == COLUMNS
    assert _rows(pg_conn) == 0
    m.upgrade()
    assert TABLE not in _tables(pg_conn)


def test_pg_an_occupied_table_refuses_and_keeps_its_row(pg_conn):
    pg_conn.exec_driver_sql(OCCUPY)
    m = _load(pg_conn)
    with pytest.raises(RuntimeError, match=r"safety_checklists holds 1 row"):
        m.upgrade()
    assert TABLE in _tables(pg_conn)
    assert _rows(pg_conn) == 1
