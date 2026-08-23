"""Migration 075 adds two provenance columns, on BOTH engines, reversibly.

Every migration here has to run on SQLite and Postgres. 073's first draft got
that wrong in a way that destroyed a money column on downgrade, so these run
the real upgrade/downgrade against a real engine rather than reading the file.

075 is additive and descriptive — it drops no data going either way — but the
SQLite branch relies on `contextlib.suppress` around an ALTER that has no
`IF NOT EXISTS`, and a suppress that swallowed the wrong thing would leave the
column silently absent. That is what a re-run has to prove.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest
from sqlalchemy import create_engine, inspect, text

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations/versions/075_price_line_provenance.py"
)


def _load(conn):
    """Import the migration with a stub `alembic.op` bound to this connection."""
    spec = importlib.util.spec_from_file_location("m075", MIGRATION)
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
        else:
            sys.modules.pop("alembic", None)
    return module


_SEED = """
CREATE TABLE job_parts_needed (id TEXT PRIMARY KEY, unit_price REAL);
CREATE TABLE invoice_lines (id TEXT PRIMARY KEY, description TEXT);
"""


@pytest.fixture
def conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm075.db'}", future=True)
    with eng.begin() as c:
        for stmt in filter(None, (s.strip() for s in _SEED.split(";"))):
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        yield c
    eng.dispose()


def _cols(conn, table: str) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns(table)}


def test_upgrade_adds_both_columns(conn):
    m = _load(conn)
    assert "price_source" not in _cols(conn, "job_parts_needed")
    assert "source" not in _cols(conn, "invoice_lines")

    m.upgrade()

    assert "price_source" in _cols(conn, "job_parts_needed")
    assert "source" in _cols(conn, "invoice_lines")


def test_existing_rows_get_null_not_a_guessed_provenance(conn):
    """NULL means "unknown", and the autodraft guard reads it as possibly-human.

    If this ever backfilled a default of 'autodraft', a re-closeout would
    delete every pre-075 line on an untouched draft — the exact bug follow-up 3
    exists to fix, reintroduced by a well-meaning default.
    """
    conn.exec_driver_sql(
        "INSERT INTO invoice_lines (id, description) VALUES ('l1', 'pre-existing')"
    )
    conn.exec_driver_sql(
        "INSERT INTO job_parts_needed (id, unit_price) VALUES ('p1', 42.00)"
    )
    _load(conn).upgrade()

    assert conn.execute(
        text("SELECT source FROM invoice_lines WHERE id='l1'")
    ).scalar() is None
    assert conn.execute(
        text("SELECT price_source FROM job_parts_needed WHERE id='p1'")
    ).scalar() is None


def test_upgrade_is_re_runnable(conn):
    """The SQLite branch has no ADD COLUMN IF NOT EXISTS and leans on
    `contextlib.suppress`. Running twice must be a no-op, not a failure — and
    must not lose the column."""
    m = _load(conn)
    m.upgrade()
    m.upgrade()
    assert "price_source" in _cols(conn, "job_parts_needed")
    assert "source" in _cols(conn, "invoice_lines")


def test_downgrade_then_upgrade_round_trips(conn):
    m = _load(conn)
    m.upgrade()
    m.downgrade()
    assert "price_source" not in _cols(conn, "job_parts_needed")
    assert "source" not in _cols(conn, "invoice_lines")

    m.upgrade()
    assert "price_source" in _cols(conn, "job_parts_needed")
    assert "source" in _cols(conn, "invoice_lines")


def test_downgrade_keeps_the_money(conn):
    """The columns are descriptive. Dropping them loses provenance — which
    cannot be recomputed — but must not touch a single amount."""
    conn.exec_driver_sql(
        "INSERT INTO job_parts_needed (id, unit_price) VALUES ('p1', 149.00)"
    )
    m = _load(conn)
    m.upgrade()
    conn.exec_driver_sql("UPDATE job_parts_needed SET price_source='catalog'")
    m.downgrade()

    assert conn.execute(
        text("SELECT unit_price FROM job_parts_needed WHERE id='p1'")
    ).scalar() == 149.00


def test_the_declared_tags_fit_the_column_widths():
    """`price_source` is VARCHAR(24) and `source` VARCHAR(16). Postgres rejects
    an over-long value; SQLite silently accepts it. That split is exactly the
    kind of thing that passes every test and then fails on deploy."""
    from gdx_dispatch.core.closeout_billing import AUTODRAFT_LINE_SOURCE
    from gdx_dispatch.core.part_pricing import PriceSource

    for key, value in vars(PriceSource).items():
        if key.startswith("_") or not isinstance(value, str):
            continue
        assert len(value) <= 24, f"PriceSource.{key} = {value!r} exceeds VARCHAR(24)"
    assert len(AUTODRAFT_LINE_SOURCE) <= 16, "invoice_lines.source is VARCHAR(16)"


def test_upgrade_and_downgrade_on_postgres(pg_test_engine):
    """The other engine. 073 proved a Postgres-only branch can hide a SQLite
    bug; this proves the Postgres branch runs at all.

    No `requires_pg` marker: that marker was removed from pytest.ini in
    v1.81.0 because nothing used it, and the `pg_test_engine` fixture already
    skips when no Postgres is reachable. A marker pytest does not know about
    is a warning, not a skip.
    """
    with pg_test_engine.begin() as c:
        c.exec_driver_sql("DROP TABLE IF EXISTS job_parts_needed CASCADE")
        c.exec_driver_sql("DROP TABLE IF EXISTS invoice_lines CASCADE")
        c.exec_driver_sql(
            "CREATE TABLE job_parts_needed (id TEXT PRIMARY KEY, unit_price NUMERIC(10,2))"
        )
        c.exec_driver_sql(
            "CREATE TABLE invoice_lines (id TEXT PRIMARY KEY, description TEXT)"
        )
        m = _load(c)
        m.upgrade()
        assert "price_source" in _cols(c, "job_parts_needed")
        assert "source" in _cols(c, "invoice_lines")
        # Re-runnable on this engine too (ADD COLUMN IF NOT EXISTS).
        m.upgrade()
        m.downgrade()
        assert "price_source" not in _cols(c, "job_parts_needed")
        assert "source" not in _cols(c, "invoice_lines")
