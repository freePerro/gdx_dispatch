"""Migration 128: ``app_settings.on_my_way_sms_enabled``.

On both engines: an existing settings row reads false (nobody is texted until
an admin opts in), up/down/up, rerunnable, a no-op on a ``create_all``
database, and a missing table is skipped.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/128_on_my_way_sms_opt_in.py"
TABLE = "app_settings"
COLUMN = "on_my_way_sms_enabled"


def _load(conn):
    spec = importlib.util.spec_from_file_location("m128", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _present(conn) -> bool:
    return COLUMN in {c["name"] for c in inspect(conn).get_columns(TABLE)}


def _legacy_schema(conn) -> None:
    """The table as an existing database has it, one row."""
    conn.exec_driver_sql("CREATE TABLE app_settings (id INTEGER PRIMARY KEY, company_name TEXT)")
    conn.exec_driver_sql("INSERT INTO app_settings (id, company_name) VALUES (1, 'Acme')")


def _value(conn):
    return conn.exec_driver_sql("SELECT on_my_way_sms_enabled FROM app_settings WHERE id = 1").scalar_one()


def test_it_chains_onto_127_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "128_on_my_way_sms_opt_in"' in source
    assert 'down_revision = "127_closeout_is_restatement"' in source
    assert len("128_on_my_way_sms_opt_in") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "127_closeout_is_restatement"', p.read_text(), re.M)
    ]
    assert revising == ["128_on_my_way_sms_opt_in.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_sqlite_existing_row_reads_off_and_round_trips(tmp_path) -> None:
    eng = create_engine(f"sqlite:///{tmp_path / 'm128.db'}", future=True)
    with eng.begin() as c:
        _legacy_schema(c)
        m = _load(c)
        m.upgrade()
        assert _present(c)
        assert not _value(c)
        m.upgrade()  # rerun is a no-op
        m.downgrade()
        assert not _present(c)
        assert c.exec_driver_sql("SELECT company_name FROM app_settings WHERE id = 1").scalar_one() == "Acme"
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert _present(c)
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
    from gdx_dispatch.models.tenant_models import AppSettings  # noqa: F401 — registers the table

    eng = create_engine(f"sqlite:///{tmp_path / 'orm.db'}", future=True)
    TenantBase.metadata.create_all(bind=eng)
    with eng.begin() as c:
        assert _present(c), "create_all already builds the column"
        _load(c).upgrade()
        assert _present(c)
    eng.dispose()


def test_postgres_existing_row_reads_false_and_round_trips(pg_test_db) -> None:
    eng = create_engine(pg_test_db, future=True)
    schema = "m128_test"
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
            assert _present(c)
            assert _value(c) is False
            m.upgrade()  # rerun is a no-op
            m.downgrade()
            c.commit()
            assert not _present(c)
            m.downgrade()  # rerun is a no-op
            m.upgrade()
            c.commit()
            assert _present(c)
            c.exec_driver_sql("DROP TABLE app_settings")
            c.commit()
            m.upgrade()  # a missing table is skipped on Postgres too
            m.downgrade()
            c.commit()
    finally:
        with eng.connect() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.commit()
        eng.dispose()
