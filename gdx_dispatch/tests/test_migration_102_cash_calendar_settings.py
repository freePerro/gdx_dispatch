"""Migration 102: tenant_forecast_settings.cash_floor + operating_account_ids,
on both engines, rerunnable, reversible, a no-op without the table, and the
existing settings row survives with both new columns NULL ("not chosen")."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/102_cash_calendar_settings.py"
TABLE = "tenant_forecast_settings"


def _load(conn):
    spec = importlib.util.spec_from_file_location("m102", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _cols(conn):
    return {c["name"] for c in inspect(conn).get_columns(TABLE)}


def test_it_chains_onto_101_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "102_cash_calendar_settings"' in source
    assert 'down_revision = "101_estimate_lead_link"' in source
    assert len("102_cash_calendar_settings") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "101_estimate_lead_link"', p.read_text(), re.M)
    ]
    assert revising == ["102_cash_calendar_settings.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_the_orm_model_carries_the_same_columns() -> None:
    from gdx_dispatch.modules.forecasting.models import ForecastSettings

    cols = ForecastSettings.__table__.columns
    assert ForecastSettings.__tablename__ == TABLE
    assert cols["cash_floor"].nullable is True
    assert cols["operating_account_ids"].nullable is True


def test_sqlite_upgrade_keeps_the_row_rerun_and_round_trip(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm102.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE tenant_forecast_settings (id CHAR(32) PRIMARY KEY, default_window_days NUMERIC(4,0) NOT NULL)"
        )
        c.exec_driver_sql("INSERT INTO tenant_forecast_settings (id, default_window_days) VALUES ('a' , 30)")
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert {"cash_floor", "operating_account_ids"} <= _cols(c)
        row = c.exec_driver_sql(
            "SELECT default_window_days, cash_floor, operating_account_ids FROM tenant_forecast_settings"
        ).one()
        assert (int(row[0]), row[1], row[2]) == (30, None, None)
        m.upgrade()  # rerun is a no-op
        m.downgrade()
        assert "cash_floor" not in _cols(c)
        assert "operating_account_ids" not in _cols(c)
        assert c.exec_driver_sql("SELECT COUNT(*) FROM tenant_forecast_settings").scalar() == 1
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert {"cash_floor", "operating_account_ids"} <= _cols(c)
    eng.dispose()


def test_sqlite_without_the_table_is_a_no_op(tmp_path):
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
    reason="the Postgres arm of 102 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@_requires_pg
def test_postgres_round_trip():
    eng = create_engine(_URL, future=True)
    schema = "m102_test"
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"SET search_path TO {schema}")
            c.exec_driver_sql(
                "CREATE TABLE tenant_forecast_settings (id uuid PRIMARY KEY, default_window_days NUMERIC(4,0) NOT NULL)"
            )
            c.exec_driver_sql(
                "INSERT INTO tenant_forecast_settings (id, default_window_days) "
                "VALUES ('11111111-1111-1111-1111-111111111111', 30)"
            )
            c.commit()
            m = _load(c)
            m.upgrade()
            c.commit()
            assert {"cash_floor", "operating_account_ids"} <= _cols(c)
            row = c.exec_driver_sql("SELECT cash_floor, operating_account_ids FROM tenant_forecast_settings").one()
            assert tuple(row) == (None, None)
            m.upgrade()
            m.downgrade()
            c.commit()
            assert "cash_floor" not in _cols(c)
            assert "operating_account_ids" not in _cols(c)
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
