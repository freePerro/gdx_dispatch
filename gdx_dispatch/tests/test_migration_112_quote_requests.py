"""Migration 112: ``quote_requests`` and ``quote_request_photos``, on both
engines: up, down and up again; rerunnable; a no-op on a database
``create_all`` built (the boot path); the columns match the ORM."""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/112_quote_requests.py"
TABLES = ("quote_requests", "quote_request_photos")


def _load(conn):
    spec = importlib.util.spec_from_file_location("m112", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _tables(conn) -> set[str]:
    insp = inspect(conn)
    return {t for t in TABLES if insp.has_table(t)}


def test_it_chains_onto_111_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "112_quote_requests"' in source
    assert 'down_revision = "111_job_cancel_lifecycle"' in source
    assert len("112_quote_requests") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "111_job_cancel_lifecycle"', p.read_text(), re.M)
    ]
    assert revising == ["112_quote_requests.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_the_migration_builds_the_orm_columns(tmp_path) -> None:
    from gdx_dispatch.modules.quote_requests.models import QuoteRequest, QuoteRequestPhoto

    eng = create_engine(f"sqlite:///{tmp_path / 'm112.db'}", future=True)
    with eng.begin() as c:
        _load(c).upgrade()
        insp = inspect(c)
        for model in (QuoteRequest, QuoteRequestPhoto):
            built = {col["name"] for col in insp.get_columns(model.__tablename__)}
            assert built == set(model.__table__.columns.keys()), model.__tablename__
    eng.dispose()


def test_sqlite_round_trip(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm112.db'}", future=True)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert _tables(c) == set(TABLES)
        m.upgrade()  # rerun is a no-op
        m.downgrade()
        assert _tables(c) == set()
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert _tables(c) == set(TABLES)
    eng.dispose()


def test_on_a_create_all_database_upgrade_is_a_no_op(tmp_path):
    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.modules.quote_requests import models  # noqa: F401

    eng = create_engine(f"sqlite:///{tmp_path / 'orm.db'}", future=True)
    TenantBase.metadata.create_all(bind=eng)
    with eng.begin() as c:
        assert _tables(c) == set(TABLES), "create_all already builds both tables"
        _load(c).upgrade()
    eng.dispose()


_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 112 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


@_requires_pg
def test_postgres_round_trip():
    eng = create_engine(_URL, future=True)
    schema = "m112_test"
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"SET search_path TO {schema}")
            m = _load(c)
            m.upgrade()
            c.commit()
            assert _tables(c) == set(TABLES)
            types = dict(
                c.exec_driver_sql(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'm112_test' AND table_name = 'quote_requests'"
                ).fetchall()
            )
            assert types["id"] == "uuid"
            assert types["doors"] == "json"
            assert types["created_at"] == "timestamp with time zone"
            m.upgrade()  # rerun is a no-op
            m.downgrade()
            c.commit()
            assert _tables(c) == set()
            m.upgrade()
            c.commit()
            assert _tables(c) == set(TABLES)
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
