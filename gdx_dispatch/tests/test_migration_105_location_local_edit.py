"""Migration 105: customer_locations.local_edit_at / local_edit_fields, on both
engines, rerunnable, reversible, a no-op without the table, and an existing
location survives with both columns NULL (no human edit recorded)."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/105_location_local_edit.py"
TABLE = "customer_locations"
COLUMNS = {"local_edit_at", "local_edit_fields"}

SEED = (
    "CREATE TABLE customer_locations (id VARCHAR(36) PRIMARY KEY, customer_id VARCHAR(36) NOT NULL, "
    "company_id VARCHAR(36) NOT NULL, address TEXT, city VARCHAR(120), state VARCHAR(20), zip VARCHAR(20))"
)
INSERT = (
    "INSERT INTO customer_locations (id, customer_id, company_id, address, city, state, zip) "
    "VALUES ('loc1', 'c1', 't1', '1 Main St', 'Anoka', 'MN', '55303')"
)


def _load(conn):
    spec = importlib.util.spec_from_file_location("m105", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _cols(conn):
    return {c["name"] for c in inspect(conn).get_columns(TABLE)}


def test_it_chains_onto_104_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "105_location_local_edit"' in source
    assert 'down_revision = "104_refuse_debit_cards"' in source
    assert len("105_location_local_edit") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "104_refuse_debit_cards"', p.read_text(), re.M)
    ]
    assert revising == ["105_location_local_edit.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_the_orm_model_carries_the_same_columns_as_customer() -> None:
    from gdx_dispatch.models.tenant_models import Customer, CustomerLocation

    for name in COLUMNS:
        loc = CustomerLocation.__table__.columns[name]
        cust = Customer.__table__.columns[name]
        assert loc.nullable is True
        assert type(loc.type) is type(cust.type), name
    assert CustomerLocation.__table__.columns["local_edit_at"].type.timezone is True


def test_sqlite_upgrade_keeps_the_row_rerun_and_round_trip(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm105.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(SEED)
        c.exec_driver_sql(INSERT)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert _cols(c) >= COLUMNS
        row = c.exec_driver_sql(
            "SELECT address, zip, local_edit_at, local_edit_fields FROM customer_locations"
        ).one()
        assert (row[0], row[1]) == ("1 Main St", "55303")
        assert row[2] is None and row[3] is None, "existing rows: no human edit recorded"
        m.upgrade()  # rerun is a no-op
        m.downgrade()
        assert not COLUMNS & _cols(c)
        assert c.exec_driver_sql("SELECT COUNT(*) FROM customer_locations").scalar() == 1
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert _cols(c) >= COLUMNS
    eng.dispose()


def test_sqlite_half_applied_adds_only_the_missing_column(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'half.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(SEED)
        c.exec_driver_sql("ALTER TABLE customer_locations ADD COLUMN local_edit_at TIMESTAMP NULL")
    with eng.begin() as c:
        _load(c).upgrade()
        assert _cols(c) >= COLUMNS
    eng.dispose()


def test_sqlite_without_the_table_is_a_no_op(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        m.downgrade()
        assert inspect(c).get_table_names() == []
    eng.dispose()


def test_orm_reads_and_writes_the_field_list_after_upgrade(tmp_path):
    from datetime import UTC, datetime

    from sqlalchemy.orm import Session

    from gdx_dispatch.models.tenant_models import CustomerLocation

    eng = create_engine(f"sqlite:///{tmp_path / 'orm.db'}", future=True)
    with eng.begin() as c:
        CustomerLocation.__table__.create(c)
        # Drop back to the pre-105 shape, then let the migration add them.
        _load(c).downgrade()
        assert not COLUMNS & _cols(c)
        c.exec_driver_sql(INSERT)
        _load(c).upgrade()
    with Session(eng) as s:
        loc = s.get(CustomerLocation, "loc1")
        assert loc.local_edit_fields is None
        loc.local_edit_fields = ["address", "zip"]
        loc.local_edit_at = datetime(2026, 10, 5, tzinfo=UTC)
        s.commit()
    with Session(eng) as s:
        loc = s.get(CustomerLocation, "loc1")
        assert loc.local_edit_fields == ["address", "zip"]
        assert loc.local_edit_at is not None
    eng.dispose()


_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 105 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@_requires_pg
def test_postgres_round_trip():
    eng = create_engine(_URL, future=True)
    schema = "m105_test"
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"SET search_path TO {schema}")
            c.exec_driver_sql(SEED)
            c.exec_driver_sql(INSERT)
            c.commit()
            m = _load(c)
            m.upgrade()
            c.commit()
            assert _cols(c) >= COLUMNS
            types = dict(
                c.exec_driver_sql(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'm105_test' AND table_name = 'customer_locations'"
                ).fetchall()
            )
            assert types["local_edit_at"] == "timestamp with time zone"
            assert types["local_edit_fields"] == "jsonb"
            assert c.exec_driver_sql(
                "SELECT local_edit_fields FROM customer_locations WHERE id = 'loc1'"
            ).scalar() is None
            m.upgrade()
            m.downgrade()
            c.commit()
            assert not COLUMNS & _cols(c)
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
