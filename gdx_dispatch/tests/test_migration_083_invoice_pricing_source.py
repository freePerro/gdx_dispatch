"""Migration 083 adds ``invoice_lines.pricing_source`` + ``pricing_inputs``.

Two dialect paths (SQLite has no ADD COLUMN IF NOT EXISTS), so both are run
here rather than one asserted and the other assumed.

The load-bearing part is idempotence in BOTH directions. On a fresh install the
ORM's ``create_all`` has already made these columns before Alembic runs, so the
SQLite branch's "duplicate column" is the expected case, not a failure — and a
migration that dies there takes the whole boot with it.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest
from sqlalchemy import create_engine, inspect

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations/versions/083_invoice_line_pricing_source.py"
)
COLUMNS = {"pricing_source", "pricing_inputs"}

# The pre-083 shape, trimmed to what the migration touches.
SEED_SQLITE = """
CREATE TABLE invoice_lines (
    id TEXT PRIMARY KEY,
    description TEXT,
    unit_price NUMERIC NOT NULL DEFAULT 0,
    cost_snapshot NUMERIC,
    source VARCHAR(16)
)
"""


def _load(conn):
    """Import the migration with ``op.get_bind()`` pointed at our connection."""
    spec = importlib.util.spec_from_file_location("m083", MIGRATION)
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


def _cols(conn) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns("invoice_lines")}


# ── Revision wiring ─────────────────────────────────────────────────────────

def test_it_chains_onto_the_current_head() -> None:
    """A migration pointing at anything but the real head forks the chain."""
    source = MIGRATION.read_text()
    assert 'revision = "083_invoice_line_pricing_source"' in source
    assert 'down_revision = "082_outlook_message_flag"' in source


def test_no_unescaped_percent_signs() -> None:
    """Alembic passes DDL through a %-formatter; a bare % is a runtime error
    that only shows up on the deploy that runs it."""
    body = MIGRATION.read_text()
    assert "%" not in body.replace("%%", "")


def test_it_does_not_touch_the_authorship_column() -> None:
    """`source` is a different axis and is read by the autodraft rebuild.
    083 must add columns beside it, never alter it."""
    body = MIGRATION.read_text()
    assert '"source"' not in body.replace('"pricing_source"', "")


# ── SQLite ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm083.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(SEED_SQLITE)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_adds_both_columns(sqlite_conn) -> None:
    mod = _load(sqlite_conn)
    assert not (COLUMNS & _cols(sqlite_conn))
    mod.upgrade()
    assert _cols(sqlite_conn) >= COLUMNS


def test_sqlite_upgrade_is_idempotent(sqlite_conn) -> None:
    """A fresh install already has these from ORM metadata — running the
    migration on top must be a no-op, not a boot failure."""
    mod = _load(sqlite_conn)
    mod.upgrade()
    mod.upgrade()  # must not raise
    assert _cols(sqlite_conn) >= COLUMNS


def test_sqlite_downgrade_removes_them(sqlite_conn) -> None:
    mod = _load(sqlite_conn)
    mod.upgrade()
    mod.downgrade()
    assert not (COLUMNS & _cols(sqlite_conn))


def test_existing_rows_survive_and_read_null(sqlite_conn) -> None:
    """NULL means "not recorded" — the migration must not invent a lane for a
    row that predates the column."""
    sqlite_conn.exec_driver_sql(
        "INSERT INTO invoice_lines (id, description, unit_price, cost_snapshot) "
        "VALUES ('l1', 'Bracket', 100, 60)"
    )
    mod = _load(sqlite_conn)
    mod.upgrade()
    row = sqlite_conn.exec_driver_sql(
        "SELECT unit_price, cost_snapshot, pricing_source, pricing_inputs "
        "FROM invoice_lines WHERE id='l1'"
    ).first()
    assert float(row[0]) == 100.0
    assert float(row[1]) == 60.0
    assert row[2] is None, "the migration guessed a lane for a pre-existing row"
    assert row[3] is None
