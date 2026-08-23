"""Migration 073 drops three columns nothing writes — and must roll back cleanly.

Every migration in this repo has to run on BOTH SQLite and Postgres. The first
draft of 073 rolled back to `amount_paid = 0` on SQLite, because only the
Postgres branch carried the rebuild — a downgrade that silently destroys a money
column. These tests run the real upgrade/downgrade against a real engine.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest
from sqlalchemy import create_engine, inspect

MIGRATION = "gdx_dispatch/migrations/versions/073_drop_dead_money_columns.py"

_SEED = """
CREATE TABLE invoices (id TEXT PRIMARY KEY, total REAL, total_amount REAL, amount_paid REAL DEFAULT 0);
CREATE TABLE payments (id TEXT PRIMARY KEY, invoice_id TEXT, amount REAL, voided_at TEXT);
CREATE TABLE jobs (id TEXT PRIMARY KEY, dispatched_at TEXT);
"""


def _load(conn):
    """Import the migration with a stub `alembic.op` bound to this connection."""
    spec = importlib.util.spec_from_file_location("m073", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    fake = types.ModuleType("alembic")

    class _Op:
        def get_bind(self):
            return conn

    fake.op = _Op()
    saved = sys.modules.get("alembic")
    sys.modules["alembic"] = fake
    try:
        spec.loader.exec_module(module)
    finally:
        if saved is not None:
            sys.modules["alembic"] = saved
    return module


@pytest.fixture
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'m073.db'}", future=True)
    with eng.begin() as conn:
        for stmt in _SEED.strip().split(";"):
            if stmt.strip():
                conn.exec_driver_sql(stmt)
        conn.exec_driver_sql("INSERT INTO invoices VALUES ('i1', 1000, NULL, 0)")
        conn.exec_driver_sql("INSERT INTO payments VALUES ('p1','i1', 250, NULL)")
        # A voided payment must NOT be rebuilt into the cache.
        conn.exec_driver_sql("INSERT INTO payments VALUES ('p2','i1', 999, '2026-01-01')")
        conn.exec_driver_sql("INSERT INTO jobs VALUES ('j1', NULL)")
        yield conn
    eng.dispose()


def _cols(conn, table):
    return {c["name"] for c in inspect(conn).get_columns(table)}


def test_upgrade_drops_all_three_columns(sqlite_conn):
    m = _load(sqlite_conn)
    assert {"total_amount", "amount_paid"} <= _cols(sqlite_conn, "invoices")

    m.upgrade()

    assert "total_amount" not in _cols(sqlite_conn, "invoices")
    assert "amount_paid" not in _cols(sqlite_conn, "invoices")
    assert "dispatched_at" not in _cols(sqlite_conn, "jobs")


def test_downgrade_rebuilds_amount_paid_from_payments_not_from_the_stale_value(sqlite_conn):
    """The rollback must leave the column better than it was, not resurrect the
    drift — and must ignore voided payments, matching _recalculate_invoice."""
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()

    assert {"total_amount", "amount_paid"} <= _cols(sqlite_conn, "invoices")
    assert "dispatched_at" in _cols(sqlite_conn, "jobs")

    paid = sqlite_conn.exec_driver_sql("SELECT amount_paid FROM invoices").scalar()
    assert float(paid) == 250.0, (
        f"rollback produced {paid} — it must rebuild from live payments (250), "
        "not zero the column and not count the voided 999"
    )
    # These two carried no data, so they come back exactly as they left: NULL.
    assert sqlite_conn.exec_driver_sql("SELECT total_amount FROM invoices").scalar() is None
    assert sqlite_conn.exec_driver_sql("SELECT dispatched_at FROM jobs").scalar() is None


def test_upgrade_is_idempotent(sqlite_conn):
    """Re-running a migration must not fail — deploys retry."""
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()
    assert "amount_paid" not in _cols(sqlite_conn, "invoices")


def test_migration_chains_onto_the_previous_head():
    """A broken revision chain is invisible until a deploy tries to run it."""
    src = pathlib.Path(MIGRATION).read_text(encoding="utf-8")
    assert 'revision = "073_drop_dead_money_columns"' in src
    assert 'down_revision = "072_invoice_source_estimate"' in src


def test_no_literal_percent_is_left_unescaped():
    """House rule: a literal % in migration SQL must be %% or the driver treats
    it as a parameter placeholder."""
    src = pathlib.Path(MIGRATION).read_text(encoding="utf-8")
    sql_lines = [ln for ln in src.splitlines() if "%" in ln]
    for line in sql_lines:
        assert "%%" in line or line.strip().startswith("#"), line
