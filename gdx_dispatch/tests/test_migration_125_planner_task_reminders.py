"""Migration 125: ``planner_tasks.remind_at`` / ``remind_user_id`` /
``reminder_sent_at``.

On both engines: existing rows read NULL in all three (no reminder), up/down/up,
rerunnable, a no-op on a ``create_all`` database, and the instants are
timezone-aware on Postgres.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import column, create_engine, insert, inspect, select, table, text

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/125_planner_task_reminders.py"
TABLE = "planner_tasks"
COLUMNS = ("remind_at", "remind_user_id", "reminder_sent_at")


def _load(conn):
    spec = importlib.util.spec_from_file_location("m125", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _present(conn) -> set[str]:
    return set(COLUMNS) & {c["name"] for c in inspect(conn).get_columns(TABLE)}


def _legacy_schema(conn) -> None:
    """The table as an existing database has it, one row."""
    conn.exec_driver_sql(f"CREATE TABLE {TABLE} (id VARCHAR(36) PRIMARY KEY, title TEXT)")
    conn.execute(insert(table(TABLE, column("id"), column("title"))).values(id="a", title="old task"))


def _values(conn) -> tuple:
    tbl = table(TABLE, column("id"), *(column(c) for c in COLUMNS))
    return tuple(conn.execute(select(*(tbl.c[c] for c in COLUMNS)).where(tbl.c.id == "a")).one())


def test_it_chains_onto_124_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "125_planner_task_reminders"' in source
    assert 'down_revision = "124_change_order_tax_rate"' in source
    assert len("125_planner_task_reminders") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "124_change_order_tax_rate"', p.read_text(), re.M)
    ]
    assert revising == ["125_planner_task_reminders.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_sqlite_existing_rows_read_null_and_round_trip(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm125.db'}", future=True)
    with eng.begin() as c:
        _legacy_schema(c)
        m = _load(c)
        m.upgrade()
        assert _present(c) == set(COLUMNS)
        assert _values(c) == (None, None, None)
        m.upgrade()  # rerun is a no-op
        m.downgrade()
        assert _present(c) == set()
        assert c.exec_driver_sql("SELECT title FROM planner_tasks WHERE id = 'a'").scalar_one() == "old task"
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert _present(c) == set(COLUMNS)
    eng.dispose()


def test_a_missing_table_is_skipped(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", future=True)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        m.downgrade()
        assert not inspect(c).has_table(TABLE)
    eng.dispose()


def test_on_a_create_all_database_upgrade_is_a_no_op(tmp_path) -> None:
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.models.tenant_models import PlannerTask  # noqa: F401 — registers the table

    eng = create_engine(f"sqlite:///{tmp_path / 'orm.db'}", future=True)
    TenantBase.metadata.create_all(bind=eng)
    with eng.begin() as c:
        assert _present(c) == set(COLUMNS), "create_all already builds the columns"
        _load(c).upgrade()
    eng.dispose()


def test_postgres_existing_rows_read_null_and_round_trip(pg_test_db) -> None:
    eng = create_engine(pg_test_db, future=True)
    schema = "m125_test"
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.exec_driver_sql(f"CREATE SCHEMA {schema}")
            c.exec_driver_sql(f"SET search_path TO {schema}")
            _legacy_schema(c)
            c.commit()
            m = _load(c)
            m.upgrade()
            c.commit()
            assert _present(c) == set(COLUMNS)
            assert _values(c) == (None, None, None)
            types = dict(c.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = :t AND is_nullable = 'YES' "
                    "AND column_default IS NULL"
                ),
                {"s": schema, "t": TABLE},
            ).all())
            assert types["remind_at"] == "timestamp with time zone"
            assert types["reminder_sent_at"] == "timestamp with time zone"
            assert types["remind_user_id"] == "character varying"
            m.upgrade()  # rerun is a no-op
            m.downgrade()
            c.commit()
            assert _present(c) == set()
            m.upgrade()
            c.commit()
            assert _present(c) == set(COLUMNS)
    finally:
        with eng.connect() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.commit()
        eng.dispose()
