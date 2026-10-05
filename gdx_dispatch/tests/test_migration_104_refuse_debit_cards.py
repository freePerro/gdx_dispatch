"""Migration 104: tenant_settings.refuse_debit_cards, on both engines,
rerunnable, reversible, a no-op without the table, and the existing settings
row survives with the new column false (debit cards still taken)."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/104_refuse_debit_cards.py"
TABLE = "tenant_settings"
COLUMN = "refuse_debit_cards"


def _load(conn):
    spec = importlib.util.spec_from_file_location("m104", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _cols(conn):
    return {c["name"] for c in inspect(conn).get_columns(TABLE)}


def test_it_chains_onto_103_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "104_refuse_debit_cards"' in source
    assert 'down_revision = "103_scheduled_sms"' in source
    assert len("104_refuse_debit_cards") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "103_scheduled_sms"', p.read_text(), re.M)
    ]
    assert revising == ["104_refuse_debit_cards.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_the_orm_model_carries_the_same_column() -> None:
    from gdx_dispatch.core.tenant_settings import TenantSettings

    col = TenantSettings.__table__.columns[COLUMN]
    assert col.nullable is False
    assert col.server_default is not None


def test_sqlite_upgrade_keeps_the_row_rerun_and_round_trip(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm104.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE tenant_settings (tenant_id CHAR(32) PRIMARY KEY, card_surcharge_percent NUMERIC(5,4))"
        )
        c.exec_driver_sql("INSERT INTO tenant_settings (tenant_id, card_surcharge_percent) VALUES ('a', 0.029)")
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert COLUMN in _cols(c)
        row = c.exec_driver_sql("SELECT card_surcharge_percent, refuse_debit_cards FROM tenant_settings").one()
        assert float(row[0]) == pytest.approx(0.029)
        assert not row[1], "existing rows default to taking debit cards"
        m.upgrade()  # rerun is a no-op
        m.downgrade()
        assert COLUMN not in _cols(c)
        assert c.exec_driver_sql("SELECT COUNT(*) FROM tenant_settings").scalar() == 1
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert COLUMN in _cols(c)
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
    reason="the Postgres arm of 104 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@_requires_pg
def test_postgres_round_trip():
    eng = create_engine(_URL, future=True)
    schema = "m104_test"
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"SET search_path TO {schema}")
            c.exec_driver_sql(
                "CREATE TABLE tenant_settings (tenant_id uuid PRIMARY KEY, card_surcharge_percent NUMERIC(5,4))"
            )
            c.exec_driver_sql(
                "INSERT INTO tenant_settings (tenant_id, card_surcharge_percent) "
                "VALUES ('11111111-1111-1111-1111-111111111111', 0.029)"
            )
            c.commit()
            m = _load(c)
            m.upgrade()
            c.commit()
            assert COLUMN in _cols(c)
            assert c.exec_driver_sql("SELECT refuse_debit_cards FROM tenant_settings").scalar() is False
            m.upgrade()
            m.downgrade()
            c.commit()
            assert COLUMN not in _cols(c)
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
