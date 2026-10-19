"""Migration 132: drop ``van_inventory`` and ``van_inventory_log`` — on both
engines, reversibly, rerunnably, all-or-nothing, and only when both are
empty. Their only writer, ``routers/van_inventory.py``, had no UI caller and
leaves in the same change (GDXA-1826), so these tests also pin that the
router, its routes and the two models are gone.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import sys
import types
from collections.abc import Generator

import pytest
from sqlalchemy import Engine, create_engine, inspect

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/132_drop_van_inventory.py"
TABLES = ("van_inventory_log", "van_inventory")

SEED = [
    "CREATE TABLE tenants (id VARCHAR(36) PRIMARY KEY, slug VARCHAR(100))",
    "INSERT INTO tenants (id, slug) VALUES ('t1', 'gdx')",
    # The shapes create_all made (tests/fixtures/structure.sql before this
    # change), minus Postgres-only types.
    "CREATE TABLE van_inventory (id VARCHAR(36) PRIMARY KEY, company_id VARCHAR(36) NOT NULL, truck_id VARCHAR(36) NOT NULL, "
    "sku VARCHAR(100), name VARCHAR(300) NOT NULL, quantity INTEGER NOT NULL, min_stock INTEGER NOT NULL, "
    "category VARCHAR(100), created_at TIMESTAMP, updated_at TIMESTAMP, deleted_at TIMESTAMP)",
    "CREATE TABLE van_inventory_log (id VARCHAR(36) PRIMARY KEY, van_inventory_id VARCHAR(36) NOT NULL, job_id VARCHAR(36), "
    "quantity_change INTEGER NOT NULL, reason VARCHAR(500), created_by VARCHAR(36) NOT NULL, created_at TIMESTAMP)",
]

_OCCUPY = {
    "van_inventory": "INSERT INTO van_inventory (id, company_id, truck_id, name, quantity, min_stock) "
    "VALUES ('v1', 't1', 'truck-1', 'hinge', 8, 0)",
    "van_inventory_log": "INSERT INTO van_inventory_log (id, van_inventory_id, quantity_change, created_by) "
    "VALUES ('l1', 'v1', -3, 'u1')",
}

EXPECTED_COLUMNS = {
    "van_inventory": {"id", "company_id", "truck_id", "sku", "name", "quantity", "min_stock", "category",
                      "created_at", "updated_at", "deleted_at"},
    "van_inventory_log": {"id", "van_inventory_id", "job_id", "quantity_change", "reason", "created_by", "created_at"},
}


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


def _cols(conn, table: str) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns(table)}


# ── Static ───────────────────────────────────────────────────────────────────

def test_it_chains_onto_131_and_131_exists() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "132_drop_van_inventory"' in source
    assert 'down_revision = "131_vacation_year_policy"' in source
    assert len("132_drop_van_inventory") <= 32, "alembic_version.version_num is varchar(32)"
    declaring = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^revision = "131_vacation_year_policy"', p.read_text(), re.M)
    ]
    assert declaring == ["131_vacation_year_policy.py"], f"exactly one migration must declare 131, found {declaring}"


def test_nothing_else_revises_131() -> None:
    """Two heads would stop `alembic upgrade head` on the next deploy."""
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "131_vacation_year_policy"', p.read_text(), re.M)
    ]
    assert revising == ["132_drop_van_inventory.py"], revising


def test_no_unescaped_percent_signs() -> None:
    body = MIGRATION.read_text()
    assert "%" not in body.replace("%%", "")


def test_no_model_declares_a_dropped_table() -> None:
    """create_all runs at every boot: the ORM must not re-create what this drops."""
    import gdx_dispatch.models  # noqa: F401 — registers every model on Base
    from gdx_dispatch.models.tenant_models import Base

    still_declared = [t for t in TABLES if t in Base.metadata.tables]
    assert not still_declared, still_declared


def test_recreate_covers_every_table_it_drops() -> None:
    m = _load(None)
    assert set(m.TABLES) == set(TABLES)
    assert set(m._RECREATE) == set(TABLES)


def test_van_inventory_router_is_retired() -> None:
    assert importlib.util.find_spec("gdx_dispatch.routers.van_inventory") is None
    routes = (pathlib.Path(__file__).resolve().parents[1] / "openapi_routes.txt").read_text()
    assert "/api/van-inventory" not in routes


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


def test_sqlite_upgrade_drops_both_and_nothing_else(sqlite_conn):
    m = _load(sqlite_conn)
    assert set(TABLES) <= _tables(sqlite_conn)
    m.upgrade()
    assert _tables(sqlite_conn) == {"tenants"}
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM tenants").scalar() == 1


@pytest.mark.parametrize("occupied", TABLES)
def test_sqlite_one_occupied_table_refuses_the_whole_drop(sqlite_conn, occupied):
    sqlite_conn.exec_driver_sql(_OCCUPY[occupied])
    m = _load(sqlite_conn)
    with pytest.raises(RuntimeError, match=rf"{occupied} holds 1 row"):
        m.upgrade()
    # All-or-nothing: the empty sibling survives too.
    assert set(TABLES) <= _tables(sqlite_conn)
    assert sqlite_conn.exec_driver_sql(f"SELECT COUNT(*) FROM {occupied}").scalar() == 1  # noqa: S608 — test constant


def test_sqlite_upgrade_is_rerunnable_and_tolerates_absence(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()
    assert _tables(sqlite_conn) == {"tenants"}


def test_sqlite_upgrade_tolerates_a_partial_set(sqlite_conn):
    sqlite_conn.exec_driver_sql("DROP TABLE van_inventory_log")
    m = _load(sqlite_conn)
    m.upgrade()
    assert _tables(sqlite_conn) == {"tenants"}


def test_sqlite_downgrade_recreates_the_model_shapes(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    for t in TABLES:
        assert _cols(sqlite_conn, t) == EXPECTED_COLUMNS[t], t
        assert sqlite_conn.exec_driver_sql(f"SELECT COUNT(*) FROM {t}").scalar() == 0  # noqa: S608 — test constant
    m.downgrade()  # rerun must not abort either
    m.upgrade()    # and the round trip closes
    assert _tables(sqlite_conn) == {"tenants"}


def test_sqlite_fresh_install_without_the_tables(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(SEED[0])
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert _tables(c) == {"tenants"}
    eng.dispose()


# ── Postgres ─────────────────────────────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 132 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    """A skipif that fails open is how "tested on both engines" quietly becomes
    false — the Postgres arm skips and the build stays green (#440)."""
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@pytest.fixture()
def pg_conn() -> Generator[Engine, None, None]:
    """Every table lives in a scratch schema — the shared CI database keeps its
    real tables."""
    schema = "m132_scratch"
    eng = create_engine(_URL, future=True)
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        c.exec_driver_sql(f"SET search_path TO {schema}")
        for stmt in SEED:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        c.exec_driver_sql(f"SET search_path TO {schema}")
        yield c
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    eng.dispose()


@_requires_pg
def test_pg_upgrade_then_downgrade_round_trip(pg_conn):
    m = _load(pg_conn)
    assert set(TABLES) <= _tables(pg_conn)
    m.upgrade()
    assert _tables(pg_conn) == {"tenants"}
    m.upgrade()  # rerunnable
    m.downgrade()
    for t in TABLES:
        assert _cols(pg_conn, t) == EXPECTED_COLUMNS[t], t
        assert pg_conn.exec_driver_sql(f"SELECT COUNT(*) FROM {t}").scalar() == 0  # noqa: S608 — test constant
    m.upgrade()
    assert _tables(pg_conn) == {"tenants"}


@_requires_pg
def test_pg_one_occupied_table_refuses_the_whole_drop(pg_conn):
    pg_conn.exec_driver_sql(_OCCUPY["van_inventory"])
    m = _load(pg_conn)
    with pytest.raises(RuntimeError, match="van_inventory holds 1 row"):
        m.upgrade()
    assert set(TABLES) <= _tables(pg_conn)
