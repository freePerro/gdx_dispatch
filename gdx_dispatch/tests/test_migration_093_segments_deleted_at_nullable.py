"""Migration 093 relaxes `segments.deleted_at` to nullable on both engines.

Why the column was NOT NULL at all: no migration ever created `segments` —
`create_all` at boot did — so the shape came straight from the ORM
annotation, and `Mapped[datetime]` with no `| None` makes SQLAlchemy 2.0
infer NOT NULL. Two things were impossible as a result: `create_segment`
never sets `deleted_at`, so every create raised IntegrityError; and every
read filters `deleted_at IS NULL`, which a NOT NULL column can never satisfy.

Both engine arms are exercised rather than one asserted and the other
assumed. On SQLite the migration goes through `batch_alter_table`, which
Alembic implements by recreating the table and copying rows — so the SQLite
arm also has to prove existing rows survive.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import types
from collections.abc import Generator

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, create_engine, inspect

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations/versions/093_segments_deleted_at_nullable.py"
)

# The pre-093 shape: exactly what create_all produced from the old annotation.
SEED_SQLITE = """
CREATE TABLE segments (
    id VARCHAR(32) NOT NULL PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    rules JSON NOT NULL,
    created_at DATETIME NOT NULL,
    deleted_at DATETIME NOT NULL
)
"""
SEED_PG = """
CREATE TABLE segments (
    id uuid PRIMARY KEY,
    name varchar(120) NOT NULL,
    rules json NOT NULL,
    created_at timestamptz NOT NULL,
    deleted_at timestamptz NOT NULL
)
"""


def _load(conn):
    """Import the migration with a real Alembic op bound to our connection.

    `batch_alter_table` needs a genuine Operations context — a stub exposing
    only `get_bind()` would make the SQLite arm untestable, which is the arm
    that does the interesting work.
    """
    spec = importlib.util.spec_from_file_location("m093", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    ops = Operations(MigrationContext.configure(conn))

    fake = types.ModuleType("alembic")
    fake.op = ops
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


def _nullable(conn) -> bool:
    schema = None
    if conn.dialect.name == "postgresql":
        schema = conn.exec_driver_sql("SELECT current_schema()").scalar()
    for col in inspect(conn).get_columns("segments", schema=schema):
        if col["name"] == "deleted_at":
            return bool(col["nullable"])
    raise AssertionError("segments.deleted_at is missing")


# ── Revision wiring ─────────────────────────────────────────────────────────

def test_it_chains_onto_the_current_head() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "093_segments_deleted_at_nullable"' in source
    assert 'down_revision = "092_drop_dead_duplicate_tables"' in source
    assert len("093_segments_deleted_at_nullable") <= 32, (
        "alembic_version.version_num is varchar(32)"
    )


def test_no_unescaped_percent_signs() -> None:
    """A literal % in migration SQL is interpolated by the DBAPI and blows up
    at runtime on Postgres. House rule: escape it as %%."""
    body = MIGRATION.read_text()
    assert "%" not in body.replace("%%", "")


# ── SQLite ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm093.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(SEED_SQLITE)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_makes_the_column_nullable(sqlite_conn):
    assert _nullable(sqlite_conn) is False
    _load(sqlite_conn).upgrade()
    assert _nullable(sqlite_conn) is True


def test_sqlite_a_live_segment_can_be_inserted_after_upgrade(sqlite_conn):
    """The whole point: create_segment does not set deleted_at."""
    _load(sqlite_conn).upgrade()
    sqlite_conn.exec_driver_sql(
        "INSERT INTO segments (id, name, rules, created_at) "
        "VALUES ('abc', 'Dormant', '{}', '2026-09-07 00:00:00')"
    )
    live = sqlite_conn.exec_driver_sql(
        "SELECT COUNT(*) FROM segments WHERE deleted_at IS NULL"
    ).scalar()
    assert live == 1


def test_sqlite_batch_alter_preserves_existing_rows(sqlite_conn):
    """batch_alter_table recreates the table on SQLite — rows must survive."""
    sqlite_conn.exec_driver_sql(
        "INSERT INTO segments (id, name, rules, created_at, deleted_at) "
        "VALUES ('keep', 'Already deleted', '{}', '2026-01-01 00:00:00', "
        "'2026-02-02 00:00:00')"
    )
    _load(sqlite_conn).upgrade()
    row = sqlite_conn.exec_driver_sql(
        "SELECT name, deleted_at FROM segments WHERE id = 'keep'"
    ).first()
    assert row[0] == "Already deleted"
    assert row[1] is not None


def test_sqlite_is_idempotent(sqlite_conn):
    """The entrypoint runs `alembic upgrade head` on every start."""
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()
    assert _nullable(sqlite_conn) is True


def test_sqlite_downgrade_restores_not_null_over_live_rows(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    sqlite_conn.exec_driver_sql(
        "INSERT INTO segments (id, name, rules, created_at) "
        "VALUES ('live', 'Live one', '{}', '2026-09-07 00:00:00')"
    )
    # A plain re-add of NOT NULL would fail on that NULL; the migration
    # stamps it first.
    m.downgrade()
    assert _nullable(sqlite_conn) is False
    stamped = sqlite_conn.exec_driver_sql(
        "SELECT deleted_at FROM segments WHERE id = 'live'"
    ).scalar()
    assert stamped is not None


def test_upgrade_is_a_no_op_when_the_table_is_absent(tmp_path):
    """No migration owns this table; on a fresh database create_all has not
    run yet, so 093 must not explode."""
    eng = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", future=True)
    with eng.begin() as c:
        _load(c).upgrade()  # must not raise
        assert not inspect(c).has_table("segments")
    eng.dispose()


# ── Postgres ────────────────────────────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 093 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    """A skipif that fails open is how "tested on both engines" quietly
    becomes false — the Postgres arm skips and the build stays green."""
    if os.environ.get("CI"):
        assert "postgresql" in _URL, (
            "CI is set but no postgres URL — the Postgres arm would skip."
        )


@pytest.fixture()
def pg_conn() -> Generator[Engine, None, None]:
    """Every table lives in a scratch schema — the shared CI database keeps its
    real tables. Same property 091 and 092 hold; `segments` is a real table
    name, so creating it in `public` would be worse here than for a table
    that only ever existed as a fixture."""
    schema = "m093_scratch"
    eng = create_engine(_URL, future=True)
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        c.exec_driver_sql(f"SET search_path TO {schema}")
        c.exec_driver_sql(SEED_PG)
    with eng.begin() as c:
        c.exec_driver_sql(f"SET search_path TO {schema}")
        yield c
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    eng.dispose()


@_requires_pg
def test_pg_upgrade_makes_the_column_nullable(pg_conn):
    assert _nullable(pg_conn) is False
    _load(pg_conn).upgrade()
    assert _nullable(pg_conn) is True


@_requires_pg
def test_pg_a_live_segment_can_be_inserted_after_upgrade(pg_conn):
    _load(pg_conn).upgrade()
    pg_conn.exec_driver_sql(
        "INSERT INTO segments (id, name, rules, created_at) VALUES "
        "('11111111-1111-4111-8111-111111111111', 'Dormant', '{}', now())"
    )
    live = pg_conn.exec_driver_sql(
        "SELECT COUNT(*) FROM segments WHERE deleted_at IS NULL"
    ).scalar()
    assert live == 1


@_requires_pg
def test_pg_downgrade_restores_not_null_over_live_rows(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    pg_conn.exec_driver_sql(
        "INSERT INTO segments (id, name, rules, created_at) VALUES "
        "('22222222-2222-4222-8222-222222222222', 'Live one', '{}', now())"
    )
    m.downgrade()
    assert _nullable(pg_conn) is False
