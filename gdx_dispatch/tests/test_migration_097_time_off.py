"""Migration 097: the time-off request table and three settings columns, on
both engines, rerunnable, reversible, tolerant of a database where
create_all has not built app_settings yet, and existing settings rows pick
up the shipped defaults (empty calendar, 480 minutes, does not count).
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

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/097_time_off.py"
COLUMNS = ("holiday_calendar", "time_off_default_minutes", "time_off_counts_toward_overtime")
TABLE = "time_off_requests"

SEED = [
    "CREATE TABLE app_settings (id VARCHAR(36) PRIMARY KEY, company_name VARCHAR(200) NOT NULL, "
    "timezone VARCHAR(100) NOT NULL)",
    "INSERT INTO app_settings (id, company_name, timezone) VALUES ('s1', 'Shop', 'America/Chicago')",
]


def _load(conn):
    spec = importlib.util.spec_from_file_location("m097", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _cols(conn, table):
    return {c["name"] for c in inspect(conn).get_columns(table)}


# ── Static ───────────────────────────────────────────────────────────────────


def test_it_chains_onto_096_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "097_time_off"' in source
    assert 'down_revision = "096_card_surcharge_columns"' in source
    assert len("097_time_off") <= 32
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "096_card_surcharge_columns"', p.read_text(), re.M)
    ]
    assert revising == ["097_time_off.py"], f"two heads would stop the next deploy: {revising}"


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


# ── SQLite ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm097.db'}", future=True)
    with eng.begin() as c:
        for stmt in SEED:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_adds_columns_with_defaults_and_the_table(sqlite_conn):
    m = _load(sqlite_conn)
    assert not (set(COLUMNS) & _cols(sqlite_conn, "app_settings"))
    assert not inspect(sqlite_conn).has_table(TABLE)
    m.upgrade()
    assert set(COLUMNS) <= _cols(sqlite_conn, "app_settings")
    assert inspect(sqlite_conn).has_table(TABLE)
    row = sqlite_conn.exec_driver_sql(
        "SELECT holiday_calendar, time_off_default_minutes, time_off_counts_toward_overtime "
        "FROM app_settings WHERE id = 's1'"
    ).one()
    assert row[0] == "[]"
    assert int(row[1]) == 480
    assert not row[2]
    cols = _cols(sqlite_conn, TABLE)
    for needed in ("id", "technician_id", "entry_type", "start_date", "end_date",
                   "minutes_per_day", "status", "requested_by", "entry_ids", "deleted_at"):
        assert needed in cols, needed


def test_sqlite_upgrade_is_rerunnable(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()
    assert set(COLUMNS) <= _cols(sqlite_conn, "app_settings")
    assert inspect(sqlite_conn).has_table(TABLE)


def test_sqlite_downgrade_drops_everything_and_the_round_trip_closes(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    assert not (set(COLUMNS) & _cols(sqlite_conn, "app_settings"))
    assert not inspect(sqlite_conn).has_table(TABLE)
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM app_settings").scalar() == 1
    m.downgrade()  # rerun must not abort
    m.upgrade()
    assert set(COLUMNS) <= _cols(sqlite_conn, "app_settings")


def test_sqlite_fresh_install_without_app_settings_still_builds_the_table(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert inspect(c).get_table_names() == [TABLE]
        m.downgrade()
        assert inspect(c).get_table_names() == []
    eng.dispose()


# ── Postgres ─────────────────────────────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 097 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@_requires_pg
def test_postgres_round_trip():
    eng = create_engine(_URL, future=True)
    schema = "m097_test"
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
            assert set(COLUMNS) <= _cols(c, "app_settings")
            assert inspect(c).has_table(TABLE)
            row = c.exec_driver_sql(
                "SELECT holiday_calendar::text, time_off_default_minutes, "
                "time_off_counts_toward_overtime FROM app_settings WHERE id = 's1'"
            ).one()
            assert row[0] == "[]"
            assert int(row[1]) == 480
            assert row[2] is False
            m.upgrade()
            m.downgrade()
            c.commit()
            assert not (set(COLUMNS) & _cols(c, "app_settings"))
            assert not inspect(c).has_table(TABLE)
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
