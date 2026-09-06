"""Migration 088: drop the six supplier-portal tables — on both engines,
reversibly, rerunnably, and only when they are empty. Every one holds 0 rows
on production (2026-09-06); the routers, models and the load-sheet view leave
in the same change.
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

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations/versions/088_drop_supplier_portal_tables.py"
)

DROPPED = {
    "supplier_catalog", "supplier_orders", "supplier_order_lines",
    "supplier_invitations", "supplier_accounts", "supplier_tenant_links",
}
NEIGHBOUR_TABLES = {"tenants", "users", "company_module_grants"}
SEED = [
    "CREATE TABLE tenants (id VARCHAR(36) PRIMARY KEY, slug VARCHAR(100))",
    "CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, email VARCHAR(255))",
    "CREATE TABLE supplier_catalog (id CHAR(36) PRIMARY KEY, company_id VARCHAR(36), supplier_name VARCHAR(200))",
    "CREATE TABLE supplier_orders (id CHAR(36) PRIMARY KEY, company_id VARCHAR(36), status VARCHAR(50))",
    "CREATE TABLE supplier_order_lines (id CHAR(36) PRIMARY KEY, order_id CHAR(36))",
    "CREATE TABLE supplier_invitations (id CHAR(36) PRIMARY KEY, token VARCHAR(100))",
    "CREATE TABLE supplier_accounts (id CHAR(36) PRIMARY KEY, email VARCHAR(254))",
    "CREATE TABLE supplier_tenant_links (id CHAR(36) PRIMARY KEY, supplier_id CHAR(36), tenant_id VARCHAR(36))",
    "INSERT INTO tenants (id, slug) VALUES ('t1', 'gdx')",
    "CREATE TABLE company_module_grants (id VARCHAR(36) PRIMARY KEY, company_id VARCHAR(36), module_key VARCHAR(100))",
    "INSERT INTO company_module_grants VALUES ('g1', 't1', 'chrome_extension')",
    "INSERT INTO company_module_grants VALUES ('g2', 't1', 'jobs')",
]


def _load(conn):
    """Import the migration with `op.get_bind()` pointed at our connection."""
    spec = importlib.util.spec_from_file_location("m088", MIGRATION)
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


def _coltype(conn, table: str, col: str) -> str:
    return str(_type(conn, table, col))


def _type(conn, table: str, col: str):
    return next(c["type"] for c in inspect(conn).get_columns(table) if c["name"] == col)


def _grant_keys(conn) -> list[str]:
    return [k for (k,) in conn.exec_driver_sql("SELECT module_key FROM company_module_grants ORDER BY module_key").fetchall()]


def test_it_chains_onto_087_and_087_exists() -> None:
    """The chain is only real if a file in this tree declares 087: merging this
    before the migration it revises would kill the next deploy's entrypoint
    ("Can't locate revision"). A source-text assertion alone cannot see that."""
    source = MIGRATION.read_text()
    assert 'revision = "088_drop_supplier_portal_tables"' in source
    assert 'down_revision = "087_drop_retired_tables"' in source
    assert len("088_drop_supplier_portal_tables") <= 32, "alembic_version.version_num is varchar(32)"
    declaring = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^revision = "087_drop_retired_tables"', p.read_text(), re.M)
    ]
    assert declaring == ["087_drop_retired_tables.py"], f"exactly one migration must declare 087, found {declaring}"


def test_no_unescaped_percent_signs() -> None:
    body = MIGRATION.read_text()
    assert "%" not in body.replace("%%", "")


def test_no_model_declares_the_dropped_tables() -> None:
    """The ORM must not re-create a table this migration drops (create_all
    runs at every boot)."""
    from gdx_dispatch.models.tenant_models import Base
    assert DROPPED.isdisjoint(Base.metadata.tables)


# ── SQLite ──────────────────────────────────────

@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm088.db'}", future=True)
    with eng.begin() as c:
        for stmt in SEED:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_drops_all_six_and_the_orphan_grant(sqlite_conn):
    m = _load(sqlite_conn)
    assert _tables(sqlite_conn) >= DROPPED
    m.upgrade()
    assert DROPPED.isdisjoint(_tables(sqlite_conn))
    assert _tables(sqlite_conn) >= NEIGHBOUR_TABLES
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM tenants").scalar() == 1
    assert _grant_keys(sqlite_conn) == ["jobs"], "the Supplier Portal Bridge grant goes, every other grant stays"


def test_sqlite_refuses_to_drop_an_occupied_table(sqlite_conn):
    """A row in any of the six means the 'nobody could reach it' assumption is
    wrong on this install: stop, keep the data, let a person look."""
    sqlite_conn.exec_driver_sql("INSERT INTO supplier_orders (id, company_id, status) VALUES ('o1', 't1', 'pending')")
    m = _load(sqlite_conn)
    with pytest.raises(RuntimeError, match="supplier_orders=1"):
        m.upgrade()
    assert _tables(sqlite_conn) >= DROPPED
    assert _grant_keys(sqlite_conn) == ["chrome_extension", "jobs"], "nothing else changes when it refuses"


def test_sqlite_upgrade_is_rerunnable_and_tolerates_absence(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()
    assert DROPPED.isdisjoint(_tables(sqlite_conn))


def test_sqlite_downgrade_recreates_the_declared_shapes(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    assert _tables(sqlite_conn) >= DROPPED
    assert _cols(sqlite_conn, "supplier_accounts") == {"id", "email", "password_hash", "company_name", "phone", "created_at"}
    assert _cols(sqlite_conn, "supplier_tenant_links") >= {"supplier_id", "tenant_id", "status"}
    assert _coltype(sqlite_conn, "supplier_accounts", "id").upper().startswith("CHAR")
    for t in DROPPED:
        assert sqlite_conn.exec_driver_sql(f"SELECT COUNT(*) FROM {t}").scalar() == 0  # noqa: S608 — table names from a test constant
    m.downgrade()  # rerun must not abort either
    m.upgrade()    # and the round trip closes


def test_sqlite_fresh_install_without_the_tables(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(SEED[0])
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert _tables(c) == {"tenants"}
    eng.dispose()


# ── Postgres ─────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 088 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@pytest.fixture()
def pg_conn() -> Generator[Engine, None, None]:
    """Every table lives in a scratch schema — the shared CI database keeps its
    real `users` / `tenants`."""
    schema = "m088_scratch"
    admin = create_engine(_URL, future=True)
    with admin.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    admin.dispose()
    eng = create_engine(_URL, future=True, connect_args={"options": f"-c search_path={schema}"})
    with eng.begin() as c:
        for stmt in SEED:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        yield c
    eng.dispose()
    admin = create_engine(_URL, future=True)
    with admin.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    admin.dispose()


@_requires_pg
def test_pg_upgrade_drops_all_six_and_the_orphan_grant(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    assert DROPPED.isdisjoint(_tables(pg_conn))
    assert _tables(pg_conn) >= NEIGHBOUR_TABLES
    assert _grant_keys(pg_conn) == ["jobs"]


@_requires_pg
def test_pg_refuses_to_drop_an_occupied_table(pg_conn):
    pg_conn.exec_driver_sql("INSERT INTO supplier_catalog (id, company_id, supplier_name) VALUES ('c1', 't1', 'Acme')")
    m = _load(pg_conn)
    with pytest.raises(RuntimeError, match="supplier_catalog=1"):
        m.upgrade()
    assert _tables(pg_conn) >= DROPPED


@_requires_pg
def test_pg_upgrade_rerun_keeps_the_transaction_healthy(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    m.upgrade()
    assert pg_conn.exec_driver_sql("SELECT COUNT(*) FROM tenants").scalar() == 1


@_requires_pg
def test_pg_downgrade_then_upgrade_round_trips_with_prod_types(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    m.downgrade()
    assert _tables(pg_conn) >= DROPPED
    assert "password_hash" in _cols(pg_conn, "supplier_accounts")
    # prod's real types (pg_dump): uuid ids, timestamptz — so an older image's
    # ORM queries bind, instead of "operator does not exist: uuid = character"
    assert _coltype(pg_conn, "supplier_accounts", "id").upper() == "UUID"
    assert _type(pg_conn, "supplier_accounts", "created_at").timezone is True, "timestamptz, as prod's pg_dump shows"
    assert _coltype(pg_conn, "supplier_tenant_links", "supplier_id").upper() == "UUID"
    m.downgrade()
    m.upgrade()
    assert DROPPED.isdisjoint(_tables(pg_conn))
