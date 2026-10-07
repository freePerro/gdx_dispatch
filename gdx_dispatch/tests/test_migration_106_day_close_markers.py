"""Migration 106: ``time_entries.day_closed_at`` and
``appointments.day_closed_at`` (multi-day jobs plan §5.4a), on both engines:
up, down and up again; rerunnable; a no-op without the tables; half-applied
adds only what is missing; existing rows keep NULL; and both on a database
``create_all`` built (the boot path, where it is a no-op) and on one it did
not."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/106_day_close_markers.py"
TABLES = ("time_entries", "appointments")
COLUMN = "day_closed_at"

SEEDS = (
    "CREATE TABLE time_entries (id VARCHAR(36) PRIMARY KEY, job_id VARCHAR(36), "
    "user_id VARCHAR(36), clock_in TIMESTAMP, duration_minutes INTEGER)",
    "CREATE TABLE appointments (id VARCHAR(36) PRIMARY KEY, job_id VARCHAR(36), "
    "start_at TIMESTAMP, status VARCHAR(20))",
)
INSERTS = (
    "INSERT INTO time_entries (id, job_id, user_id, clock_in, duration_minutes) "
    "VALUES ('te1', 'j1', 'u1', '2026-10-05 08:00:00', 480)",
    "INSERT INTO appointments (id, job_id, start_at, status) "
    "VALUES ('a1', 'j1', '2026-10-05 08:00:00', 'arrived')",
)


def _load(conn):
    spec = importlib.util.spec_from_file_location("m106", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _has(conn, table: str) -> bool:
    return COLUMN in {c["name"] for c in inspect(conn).get_columns(table)}


def _seed(c):
    for sql in SEEDS + INSERTS:
        c.exec_driver_sql(sql)


def test_it_chains_onto_105_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "106_day_close_markers"' in source
    assert 'down_revision = "105_location_local_edit"' in source
    assert len("106_day_close_markers") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "105_location_local_edit"', p.read_text(), re.M)
    ]
    assert revising == ["106_day_close_markers.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


def test_the_orm_models_carry_the_column() -> None:
    from gdx_dispatch.models.tenant_models import Appointment, TimeEntry

    for model in (TimeEntry, Appointment):
        col = model.__table__.columns[COLUMN]
        assert col.nullable is True
        assert col.type.timezone is True, model.__name__


def test_sqlite_upgrade_keeps_rows_rerun_and_round_trip(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm106.db'}", future=True)
    with eng.begin() as c:
        _seed(c)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert all(_has(c, t) for t in TABLES)
        assert c.exec_driver_sql("SELECT duration_minutes, day_closed_at FROM time_entries").one() == (480, None)
        assert c.exec_driver_sql("SELECT status, day_closed_at FROM appointments").one() == ("arrived", None)
        m.upgrade()  # rerun is a no-op
        m.downgrade()
        assert not any(_has(c, t) for t in TABLES)
        assert c.exec_driver_sql("SELECT COUNT(*) FROM time_entries").scalar() == 1
        assert c.exec_driver_sql("SELECT COUNT(*) FROM appointments").scalar() == 1
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert all(_has(c, t) for t in TABLES)
    eng.dispose()


def test_sqlite_half_applied_adds_only_the_missing_column(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'half.db'}", future=True)
    with eng.begin() as c:
        _seed(c)
        c.exec_driver_sql("ALTER TABLE time_entries ADD COLUMN day_closed_at TIMESTAMP NULL")
    with eng.begin() as c:
        _load(c).upgrade()
        assert all(_has(c, t) for t in TABLES)
    eng.dispose()


def test_sqlite_without_the_tables_is_a_no_op(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        m.downgrade()
        assert inspect(c).get_table_names() == []
    eng.dispose()


def test_on_a_create_all_database_upgrade_is_a_no_op_and_the_orm_round_trips(tmp_path):
    from datetime import UTC, datetime
    from uuid import uuid4

    from sqlalchemy.orm import Session

    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.models.tenant_models import TimeEntry

    eng = create_engine(f"sqlite:///{tmp_path / 'orm.db'}", future=True)
    TenantBase.metadata.create_all(bind=eng)  # the boot path, every table
    with eng.begin() as c:
        assert all(_has(c, t) for t in TABLES), "create_all already has the column"
        _load(c).upgrade()  # the boot path: a no-op
        assert all(_has(c, t) for t in TABLES)
        # Back to the pre-106 shape and up again, through the ORM tables.
        _load(c).downgrade()
        assert not any(_has(c, t) for t in TABLES)
        _load(c).upgrade()
    marker = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)
    with Session(eng) as s:
        te = TimeEntry(id=uuid4(), company_id="t1", tech_id="tech1", clock_in=marker,
                       entry_type="work", day_closed_at=marker)
        s.add(te)
        s.commit()
        tid = te.id
    with Session(eng) as s:
        assert s.get(TimeEntry, tid).day_closed_at is not None
    eng.dispose()


_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 106 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@_requires_pg
def test_postgres_round_trip():
    eng = create_engine(_URL, future=True)
    schema = "m106_test"
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"SET search_path TO {schema}")
            _seed(c)
            c.commit()
            m = _load(c)
            m.upgrade()
            c.commit()
            assert all(_has(c, t) for t in TABLES)
            types = dict(
                c.exec_driver_sql(
                    "SELECT table_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'm106_test' AND column_name = 'day_closed_at'"
                ).fetchall()
            )
            assert types == {t: "timestamp with time zone" for t in TABLES}
            assert c.exec_driver_sql("SELECT day_closed_at FROM time_entries").scalar() is None
            m.upgrade()
            m.downgrade()
            c.commit()
            assert not any(_has(c, t) for t in TABLES)
            m.upgrade()
            c.commit()
            assert all(_has(c, t) for t in TABLES)
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
