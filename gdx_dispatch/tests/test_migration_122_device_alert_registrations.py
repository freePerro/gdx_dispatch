"""Migration 122: device_alert_registrations, on both engines — creates the
table matching the ORM, rerunnable, reversible, and a no-op where
``create_all`` already built it."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/122_device_alert_registrations.py"
TABLE = "device_alert_registrations"


def _load(conn):
    spec = importlib.util.spec_from_file_location("m122", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _cols(conn):
    return {c["name"]: c for c in inspect(conn).get_columns(TABLE)}


def test_it_chains_onto_121_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "122_device_alert_registrations"' in source
    assert 'down_revision = "121_trade_estimate_tax_rate_null"' in source
    assert len("122_device_alert_registrations") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "121_trade_estimate_tax_rate_null"', p.read_text(), re.M)
    ]
    assert revising == ["122_device_alert_registrations.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_migration_matches_the_orm(tmp_path):
    from gdx_dispatch.models.tenant_models import DeviceAlertRegistration

    eng = create_engine(f"sqlite:///{tmp_path / 'm122.db'}", future=True)
    with eng.begin() as c:
        _load(c).upgrade()
        cols = _cols(c)
        orm = DeviceAlertRegistration.__table__.columns
        assert set(cols) == {col.name for col in orm}
        for col in orm:
            assert cols[col.name]["nullable"] == col.nullable, col.name
        uniques = inspect(c).get_unique_constraints(TABLE) + [
            i for i in inspect(c).get_indexes(TABLE) if i.get("unique")
        ]
        assert any(u["column_names"] == ["token_hash"] for u in uniques)
    eng.dispose()


def test_sqlite_rerun_and_round_trip(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm122.db'}", future=True)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        m.upgrade()  # rerun is a no-op
        assert TABLE in inspect(c).get_table_names()
        m.downgrade()
        assert TABLE not in inspect(c).get_table_names()
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert TABLE in inspect(c).get_table_names()
    eng.dispose()


_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 122 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@_requires_pg
def test_postgres_round_trip():
    eng = create_engine(_URL, future=True)
    schema = "m122_test"
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"SET search_path TO {schema}")
            c.commit()
            m = _load(c)
            m.upgrade()
            c.commit()
            assert "token_hash" in _cols(c)
            c.exec_driver_sql(
                "INSERT INTO device_alert_registrations (id, user_id, token_hash, created_at) "
                "VALUES ('a', 'u', 'h', now())"
            )
            c.commit()
            m.upgrade()
            m.downgrade()
            c.commit()
            assert TABLE not in inspect(c).get_table_names()
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
