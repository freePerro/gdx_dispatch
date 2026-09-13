"""Migration 094 brings three live columns to the ORM's NOT NULL (#676).

`invoices.customer_id`, `invoices.totals_locked` and
`game_events.created_by_user_id` are declared NOT NULL by the ORM and were
nullable on real databases. A pave rebuilds from the ORM, and a NULL cannot be
reloaded into it.

The property that matters most is the one a naive `ALTER ... SET NOT NULL`
lacks: **a column still holding NULLs is skipped, never raised over** — the
entrypoint runs `alembic upgrade head` under `set -e`, so raising crash-loops
the app. Single-head wiring and revision-id length are enforced for every
migration by test_migration_revision_ids.py, so they are not repeated here.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import pathlib
import sys
import types
from collections.abc import Generator

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, create_engine, exc, inspect

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations/versions/094_not_null_to_match_orm.py"
)

ALL_NULLABLE = {
    ("invoices", "customer_id"): True,
    ("invoices", "totals_locked"): True,
    ("game_events", "created_by_user_id"): True,
}
ALL_NOT_NULL = dict.fromkeys(ALL_NULLABLE, False)

# The drifted shape: every target nullable.
SEED_SQLITE = (
    "CREATE TABLE invoices (id CHAR(32) NOT NULL PRIMARY KEY, "
    "invoice_number VARCHAR(50) NOT NULL, customer_id CHAR(32), totals_locked BOOLEAN)",
    "CREATE TABLE game_events (id CHAR(32) NOT NULL PRIMARY KEY, "
    "actor_id VARCHAR(100) NOT NULL, created_by_user_id VARCHAR(100))",
)
SEED_PG = (
    "CREATE TABLE invoices (id uuid PRIMARY KEY, invoice_number varchar(50) NOT NULL, "
    "customer_id uuid, totals_locked boolean)",
    "CREATE TABLE game_events (id uuid PRIMARY KEY, actor_id varchar(100) NOT NULL, "
    "created_by_user_id varchar(100))",
)


def _load(conn):
    """Import the migration with a real Alembic op bound to our connection."""
    spec = importlib.util.spec_from_file_location("m094", MIGRATION)
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


def _nullable(conn) -> dict[tuple[str, str], bool]:
    schema = None
    if conn.dialect.name == "postgresql":
        schema = conn.exec_driver_sql("SELECT current_schema()").scalar()
    insp = inspect(conn)
    out = {}
    for table, column in ALL_NULLABLE:
        for col in insp.get_columns(table, schema=schema):
            if col["name"] == column:
                out[(table, column)] = bool(col["nullable"])
    return out


def test_no_unescaped_percent_signs_in_sql() -> None:
    """A literal % in migration SQL is interpolated by the DBAPI on Postgres.
    The file's only % are logging placeholders, which never reach SQL."""
    sql_lines = [
        line for line in MIGRATION.read_text().splitlines()
        if "SELECT" in line or "ALTER TABLE" in line or "UPDATE" in line
    ]
    assert sql_lines, "the SQL-line filter matched nothing, so it checks nothing"
    assert all("%" not in line.replace("%%", "") for line in sql_lines)


def test_every_target_is_declared_not_null_by_the_orm(tmp_path) -> None:
    """094 exists to match the model. If a model relaxes one of these, 094 is
    now wrong for it — and a target missing from the ORM is a typo."""
    import gdx_dispatch.models  # noqa: F401 — registers every model
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.core.tenant_settings import Base

    eng = create_engine(f"sqlite:///{tmp_path / 'x.db'}", future=True)
    with eng.connect() as c:
        targets = _load(c)._TARGETS
    eng.dispose()
    assert {(t.table, t.column) for t in targets} == set(ALL_NULLABLE)
    for t in targets:
        table = TenantBase.metadata.tables.get(t.table)
        if table is None:
            table = Base.metadata.tables.get(t.table)
        assert table is not None, t.table
        assert table.c[t.column].nullable is False, f"{t.table}.{t.column}"


# ── SQLite ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm094.db'}", future=True)
    with eng.begin() as c:
        for ddl in SEED_SQLITE:
            c.exec_driver_sql(ddl)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_tightens_every_target(sqlite_conn):
    assert _nullable(sqlite_conn) == ALL_NULLABLE
    _load(sqlite_conn).upgrade()
    assert _nullable(sqlite_conn) == ALL_NOT_NULL


@pytest.mark.parametrize("insert", [
    "INSERT INTO invoices (id, invoice_number, customer_id, totals_locked) "
    "VALUES ('a', 'INV-1', NULL, 0)",
    "INSERT INTO invoices (id, invoice_number, customer_id, totals_locked) "
    "VALUES ('b', 'INV-2', 'c', NULL)",
    "INSERT INTO game_events (id, actor_id, created_by_user_id) VALUES ('e', 'x', NULL)",
])
def test_sqlite_a_null_is_refused_after_upgrade(sqlite_conn, insert):
    """The constraint can actually fail for the defect it exists to stop."""
    _load(sqlite_conn).upgrade()
    with pytest.raises(exc.IntegrityError):
        sqlite_conn.exec_driver_sql(insert)


def test_sqlite_batch_alter_preserves_existing_rows(sqlite_conn):
    sqlite_conn.exec_driver_sql(
        "INSERT INTO invoices (id, invoice_number, customer_id, totals_locked) "
        "VALUES ('keep', 'INV-7', '11111111111141118111111111111111', 1)"
    )
    sqlite_conn.exec_driver_sql(
        "INSERT INTO game_events (id, actor_id, created_by_user_id) VALUES ('g', 'tech', 'u1')"
    )
    _load(sqlite_conn).upgrade()
    inv = sqlite_conn.exec_driver_sql(
        "SELECT invoice_number, customer_id, totals_locked FROM invoices"
    ).all()
    assert [tuple(r) for r in inv] == [("INV-7", "11111111111141118111111111111111", 1)]
    ev = sqlite_conn.exec_driver_sql("SELECT actor_id, created_by_user_id FROM game_events").all()
    assert [tuple(r) for r in ev] == [("tech", "u1")]


def test_sqlite_a_column_holding_nulls_is_skipped_not_raised(sqlite_conn, caplog):
    """The crash-loop guard: a NULL left behind must never take the app down,
    and must not stop the clean columns — same table or another — tightening."""
    sqlite_conn.exec_driver_sql(
        "INSERT INTO invoices (id, invoice_number, customer_id, totals_locked) "
        "VALUES ('orphan', 'INV-9', NULL, 0)"
    )
    with caplog.at_level(logging.WARNING, logger="alembic.runtime.migration"):
        _load(sqlite_conn).upgrade()  # must not raise
    assert _nullable(sqlite_conn) == {**ALL_NOT_NULL, ("invoices", "customer_id"): True}
    assert "invoices.customer_id holds 1 NULL row" in caplog.text
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM invoices").scalar() == 1


def test_sqlite_is_idempotent(sqlite_conn):
    """The entrypoint runs `alembic upgrade head` on every start."""
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()
    assert _nullable(sqlite_conn) == ALL_NOT_NULL


def test_sqlite_downgrade_changes_nothing(sqlite_conn):
    """There was no single pre-094 shape (customer_id was nullable on prod,
    NOT NULL on fresh installs), so any relaxing downgrade loosens some
    install beyond anything it has been. Rollback is pinning the old image."""
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    assert _nullable(sqlite_conn) == ALL_NOT_NULL


def test_sqlite_downgrade_leaves_a_fresh_install_not_null(tmp_path):
    """The case round-2 review broke: a table that was already NOT NULL
    before 094 must not come out of a downgrade nullable."""
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE invoices (id CHAR(32) PRIMARY KEY, "
            "customer_id CHAR(32) NOT NULL, totals_locked BOOLEAN NOT NULL)"
        )
        c.exec_driver_sql(
            "CREATE TABLE game_events (id CHAR(32) PRIMARY KEY, "
            "created_by_user_id VARCHAR(100) NOT NULL)"
        )
        m = _load(c)
        m.upgrade()
        m.downgrade()
        assert _nullable(c) == ALL_NOT_NULL
    eng.dispose()


def test_upgrade_is_a_no_op_when_the_tables_are_absent(tmp_path):
    """GDX_SKIP_BOOTSTRAP=1 or a bare `alembic upgrade` reaches 094 before
    create_all has built these tables."""
    eng = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", future=True)
    with eng.begin() as c:
        _load(c).upgrade()  # must not raise
        assert not inspect(c).has_table("invoices")
    eng.dispose()


def test_upgrade_leaves_already_matching_tables_untouched(tmp_path):
    """Demo's invoices and every freshly built one already match; 094 must not
    rebuild them."""
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE invoices (id CHAR(32) PRIMARY KEY, "
            "customer_id CHAR(32) NOT NULL, totals_locked BOOLEAN NOT NULL)"
        )
        ddl = "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'invoices'"
        before = c.exec_driver_sql(ddl).scalar()
        _load(c).upgrade()
        # A SQLite batch rebuild rewrites the stored CREATE TABLE text (it
        # keeps indexes, so an index check could not tell); unchanged text
        # means the table was never recreated.
        assert c.exec_driver_sql(ddl).scalar() == before
    eng.dispose()


# ── Postgres ────────────────────────────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 094 needs a real Postgres; set DATABASE_URL "
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
    """A scratch schema: both are real table names, and the shared CI database
    keeps its real tables."""
    schema = "m094_scratch"
    eng = create_engine(_URL, future=True)
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        c.exec_driver_sql(f"SET search_path TO {schema}")
        for ddl in SEED_PG:
            c.exec_driver_sql(ddl)
    with eng.begin() as c:
        c.exec_driver_sql(f"SET search_path TO {schema}")
        yield c
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    eng.dispose()


@_requires_pg
def test_pg_upgrade_tightens_every_target_and_refuses_a_null(pg_conn):
    pg_conn.exec_driver_sql(
        "INSERT INTO invoices (id, invoice_number, customer_id, totals_locked) VALUES "
        "('22222222-2222-4222-8222-222222222222', 'INV-1', "
        "'11111111-1111-4111-8111-111111111111', false)"
    )
    _load(pg_conn).upgrade()
    assert _nullable(pg_conn) == ALL_NOT_NULL
    nested = pg_conn.begin_nested()
    with pytest.raises(exc.IntegrityError):
        pg_conn.exec_driver_sql(
            "INSERT INTO invoices (id, invoice_number, customer_id, totals_locked) VALUES "
            "('33333333-3333-4333-8333-333333333333', 'INV-2', NULL, false)"
        )
    nested.rollback()
    assert pg_conn.exec_driver_sql("SELECT COUNT(*) FROM invoices").scalar() == 1


@_requires_pg
def test_pg_a_column_holding_nulls_is_skipped_not_raised(pg_conn):
    pg_conn.exec_driver_sql(
        "INSERT INTO invoices (id, invoice_number, customer_id, totals_locked) VALUES "
        "('44444444-4444-4444-8444-444444444444', 'INV-3', NULL, false)"
    )
    _load(pg_conn).upgrade()  # must not raise
    assert _nullable(pg_conn) == {**ALL_NOT_NULL, ("invoices", "customer_id"): True}


@_requires_pg
def test_pg_downgrade_changes_nothing(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    m.downgrade()
    assert _nullable(pg_conn) == ALL_NOT_NULL
