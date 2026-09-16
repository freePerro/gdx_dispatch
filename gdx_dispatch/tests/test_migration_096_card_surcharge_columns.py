"""Migration 096: two nullable columns for the card surcharge, on both engines,
rerunnable, reversible, and tolerant of a database where create_all has not
built the tables yet (a fresh install builds them from the model instead).
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/096_card_surcharge_columns.py"
COLUMNS = (("payments", "surcharge_amount"), ("tenant_settings", "card_surcharge_percent"))

SEED = [
    "CREATE TABLE payments (id VARCHAR(36) PRIMARY KEY, invoice_id VARCHAR(36) NOT NULL, amount NUMERIC(12,2) NOT NULL, "
    "method VARCHAR(50) NOT NULL, company_id VARCHAR(36) NOT NULL)",
    "INSERT INTO payments (id, invoice_id, amount, method, company_id) VALUES ('p1', 'i1', 100.00, 'check', 't1')",
    "CREATE TABLE tenant_settings (tenant_id VARCHAR(36) PRIMARY KEY, late_fee_percent NUMERIC(5,4))",
    "INSERT INTO tenant_settings (tenant_id) VALUES ('t1')",
]


def _load(conn):
    """Import the migration and bind alembic's `op` to our connection."""
    spec = importlib.util.spec_from_file_location("m096", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _cols(conn, table):
    return {c["name"] for c in inspect(conn).get_columns(table)}


def _has(conn, table, column):
    return column in _cols(conn, table)


# ── Static ───────────────────────────────────────────────────────────────────


def test_it_chains_onto_095_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "096_card_surcharge_columns"' in source
    assert 'down_revision = "095_drop_campaigns_mobile_sync"' in source
    assert len("096_card_surcharge_columns") <= 32, "alembic_version.version_num is varchar(32)"
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "095_drop_campaigns_mobile_sync"', p.read_text(), re.M)
    ]
    assert revising == ["096_card_surcharge_columns.py"], f"two heads would stop the next deploy: {revising}"


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


# ── SQLite ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm096.db'}", future=True)
    with eng.begin() as c:
        for stmt in SEED:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_adds_both_columns_nullable_and_keeps_rows(sqlite_conn):
    m = _load(sqlite_conn)
    for table, column in COLUMNS:
        assert not _has(sqlite_conn, table, column)
    m.upgrade()
    for table, column in COLUMNS:
        assert _has(sqlite_conn, table, column), (table, column)
        col = next(c for c in inspect(sqlite_conn).get_columns(table) if c["name"] == column)
        assert col["nullable"] is True
    assert sqlite_conn.exec_driver_sql("SELECT surcharge_amount FROM payments WHERE id = 'p1'").scalar() is None
    assert sqlite_conn.exec_driver_sql("SELECT card_surcharge_percent FROM tenant_settings").scalar() is None


def test_sqlite_upgrade_is_rerunnable(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()
    for table, column in COLUMNS:
        assert _has(sqlite_conn, table, column)


def test_sqlite_downgrade_drops_both_and_the_round_trip_closes(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    for table, column in COLUMNS:
        assert not _has(sqlite_conn, table, column), (table, column)
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM payments").scalar() == 1
    m.downgrade()  # rerun must not abort
    m.upgrade()
    for table, column in COLUMNS:
        assert _has(sqlite_conn, table, column)


def test_sqlite_fresh_install_without_the_tables_is_a_no_op(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        m.downgrade()
        assert inspect(c).get_table_names() == []
    eng.dispose()


# ── Postgres ─────────────────────────────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 096 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@_requires_pg
def test_postgres_round_trip():
    eng = create_engine(_URL, future=True)
    schema = "m096_test"
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"SET search_path TO {schema}")
            for stmt in SEED:
                c.exec_driver_sql(stmt)
            c.commit()
            m = _load(c)
            m.upgrade()
            c.commit()
            for table, column in COLUMNS:
                assert _has(c, table, column), (table, column)
            m.upgrade()
            m.downgrade()
            c.commit()
            for table, column in COLUMNS:
                assert not _has(c, table, column), (table, column)
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
