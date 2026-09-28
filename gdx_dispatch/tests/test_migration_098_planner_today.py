"""Migration 098: ``planner_tasks.today_date`` and the ``planner_day_notes``
table, on both engines, rerunnable, reversible, tolerant of a database where
create_all has not built planner_tasks yet, and existing tasks come through
untouched with no Today pin.
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

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/098_planner_today.py"
TASKS = "planner_tasks"
NOTES = "planner_day_notes"

SEED = [
    "CREATE TABLE planner_tasks (id VARCHAR(36) PRIMARY KEY, company_id VARCHAR(36) NOT NULL, "
    "title VARCHAR(300) NOT NULL, status VARCHAR(20) NOT NULL, created_by VARCHAR(36) NOT NULL)",
    "INSERT INTO planner_tasks (id, company_id, title, status, created_by) "
    "VALUES ('t1', 'c1', 'Existing task', 'todo', 'u1')",
]


def _load(conn):
    spec = importlib.util.spec_from_file_location("m098", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _cols(conn, table):
    return {c["name"] for c in inspect(conn).get_columns(table)}


# ── Static ───────────────────────────────────────────────────────────────────


def test_it_chains_onto_097_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "098_planner_today"' in source
    assert 'down_revision = "097_time_off"' in source
    assert len("098_planner_today") <= 32
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "097_time_off"', p.read_text(), re.M)
    ]
    assert revising == ["098_planner_today.py"], f"two heads would stop the next deploy: {revising}"


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


# ── SQLite ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm098.db'}", future=True)
    with eng.begin() as c:
        for stmt in SEED:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_adds_the_column_and_the_table(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    assert "today_date" in _cols(sqlite_conn, TASKS)
    row = sqlite_conn.exec_driver_sql("SELECT title, today_date FROM planner_tasks WHERE id = 't1'").one()
    assert row == ("Existing task", None)
    cols = _cols(sqlite_conn, NOTES)
    for needed in ("id", "company_id", "user_id", "note_date", "body", "updated_by",
                   "updated_via", "created_at", "updated_at"):
        assert needed in cols, needed
    uniques = inspect(sqlite_conn).get_unique_constraints(NOTES)
    assert [sorted(u["column_names"]) for u in uniques] == [["note_date", "user_id"]]


def test_sqlite_upgrade_is_rerunnable(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()
    assert "today_date" in _cols(sqlite_conn, TASKS)
    assert inspect(sqlite_conn).has_table(NOTES)


def test_sqlite_downgrade_drops_both_and_keeps_the_tasks(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    assert "today_date" not in _cols(sqlite_conn, TASKS)
    assert not inspect(sqlite_conn).has_table(NOTES)
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM planner_tasks").scalar() == 1
    m.downgrade()  # rerun must not abort
    m.upgrade()
    assert "today_date" in _cols(sqlite_conn, TASKS)


def test_sqlite_fresh_install_without_planner_tasks_still_builds_the_notes_table(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert inspect(c).get_table_names() == [NOTES]
        m.downgrade()
        assert inspect(c).get_table_names() == []
    eng.dispose()


# ── Postgres ─────────────────────────────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 098 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@_requires_pg
def test_postgres_round_trip():
    eng = create_engine(_URL, future=True)
    schema = "m098_test"
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
            assert "today_date" in _cols(c, TASKS)
            assert inspect(c).has_table(NOTES)
            row = c.exec_driver_sql("SELECT today_date FROM planner_tasks WHERE id = 't1'").one()
            assert row[0] is None
            m.upgrade()
            m.downgrade()
            c.commit()
            assert "today_date" not in _cols(c, TASKS)
            assert not inspect(c).has_table(NOTES)
            assert c.exec_driver_sql("SELECT COUNT(*) FROM planner_tasks").scalar() == 1
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
