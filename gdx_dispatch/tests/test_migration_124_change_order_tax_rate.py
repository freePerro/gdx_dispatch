"""Migration 124: ``change_orders.tax_rate`` (GDXA-795).

On both engines: existing rows read NULL (the legacy live-resolve path),
up/down/up, rerunnable, a no-op on a ``create_all`` database, and the column
is the ``Numeric(9, 6)`` the estimate and invoice rates use.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
from decimal import Decimal

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import column, create_engine, insert, inspect, select, table, text, update

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/124_change_order_tax_rate.py"
TABLE = "change_orders"
COLUMN = "tax_rate"


def _load(conn):
    spec = importlib.util.spec_from_file_location("m124", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _has_column(conn) -> bool:
    return COLUMN in {c["name"] for c in inspect(conn).get_columns(TABLE)}


def _t():
    return table(TABLE, column("id"), column("title"), column(COLUMN))


def _legacy_schema(conn) -> None:
    """The table as an existing database has it, one row."""
    conn.exec_driver_sql(f"CREATE TABLE {TABLE} (id VARCHAR(36) PRIMARY KEY, title TEXT)")
    conn.execute(insert(table(TABLE, column("id"), column("title"))).values(id="a", title="old co"))


def _value(conn) -> object:
    tbl = _t()
    return conn.execute(select(tbl.c[COLUMN]).where(tbl.c.id == "a")).scalar_one()


def test_it_chains_onto_123_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "124_change_order_tax_rate"' in source
    assert 'down_revision = "123_audit_chain_serialized"' in source
    assert len("124_change_order_tax_rate") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "123_audit_chain_serialized"', p.read_text(), re.M)
    ]
    assert revising == ["124_change_order_tax_rate.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_sqlite_existing_rows_read_null_and_round_trip(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm124.db'}", future=True)
    with eng.begin() as c:
        _legacy_schema(c)
        m = _load(c)
        m.upgrade()
        assert _has_column(c)
        assert _value(c) is None
        m.upgrade()  # rerun is a no-op
        m.downgrade()
        assert not _has_column(c)
        assert c.exec_driver_sql("SELECT title FROM change_orders WHERE id = 'a'").scalar_one() == "old co"
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert _has_column(c)
    eng.dispose()


def test_on_a_create_all_database_upgrade_is_a_no_op(tmp_path) -> None:
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.routers.change_orders import ChangeOrder  # noqa: F401 — registers the table

    eng = create_engine(f"sqlite:///{tmp_path / 'orm.db'}", future=True)
    TenantBase.metadata.create_all(bind=eng)
    with eng.begin() as c:
        assert _has_column(c), "create_all already builds the column"
        _load(c).upgrade()
    eng.dispose()


def test_postgres_existing_rows_read_null_and_round_trip(pg_test_db) -> None:
    eng = create_engine(pg_test_db, future=True)
    schema = "m124_test"
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
            assert _has_column(c)
            assert _value(c) is None
            info = c.execute(
                text(
                    "SELECT data_type, numeric_precision, numeric_scale, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = :t AND column_name = :c"
                ),
                {"s": schema, "t": TABLE, "c": COLUMN},
            ).one()
            assert tuple(info) == ("numeric", 9, 6, "YES", None), info
            c.execute(update(_t()).values({COLUMN: Decimal("0.07375")}))
            assert _value(c) == Decimal("0.073750")
            m.upgrade()  # rerun is a no-op
            m.downgrade()
            c.commit()
            assert not _has_column(c)
            m.upgrade()
            c.commit()
            assert _has_column(c)
    finally:
        with eng.connect() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.commit()
        eng.dispose()
