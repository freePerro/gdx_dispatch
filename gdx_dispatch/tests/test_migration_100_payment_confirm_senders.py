"""Migration 100: the payment-confirmation sender list on outlook_settings, on
both engines, rerunnable, reversible, a no-op where create_all has not built
outlook_settings, and an existing settings row starts with the feature OFF."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/100_payment_confirm_senders.py"
COLUMN = "payment_confirmation_sender_allowlist"
SEED = [
    "CREATE TABLE outlook_settings (id INTEGER PRIMARY KEY, backfill_days INTEGER NOT NULL)",
    "INSERT INTO outlook_settings (id, backfill_days) VALUES (1, 90)",
]


def _load(conn):
    spec = importlib.util.spec_from_file_location("m100", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _cols(conn):
    return {c["name"] for c in inspect(conn).get_columns("outlook_settings")}


def test_it_chains_onto_099_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "100_payment_confirm_senders"' in source
    assert 'down_revision = "099_lead_intake"' in source
    assert len("100_payment_confirm_senders") <= 32
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "099_lead_intake"', p.read_text(), re.M)
    ]
    assert revising == ["100_payment_confirm_senders.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm100.db'}", future=True)
    with eng.begin() as c:
        for stmt in SEED:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_adds_an_empty_list_rerun_and_round_trip(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    assert COLUMN in _cols(sqlite_conn)
    raw = sqlite_conn.exec_driver_sql("SELECT payment_confirmation_sender_allowlist FROM outlook_settings").scalar()
    assert json.loads(raw) == []
    m.upgrade()
    m.downgrade()
    assert COLUMN not in _cols(sqlite_conn)
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM outlook_settings").scalar() == 1
    m.downgrade()
    m.upgrade()
    assert COLUMN in _cols(sqlite_conn)


def test_sqlite_without_outlook_settings_is_a_no_op(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        m.downgrade()
        assert inspect(c).get_table_names() == []
    eng.dispose()


_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 100 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@_requires_pg
def test_postgres_round_trip():
    eng = create_engine(_URL, future=True)
    schema = "m100_test"
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
            assert COLUMN in _cols(c)
            assert c.exec_driver_sql(
                "SELECT payment_confirmation_sender_allowlist::text FROM outlook_settings"
            ).scalar() == "[]"
            m.upgrade()
            m.downgrade()
            c.commit()
            assert COLUMN not in _cols(c)
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
