"""Migration 111: a job records when it was cancelled and why
(``jobs.cancelled_at``, ``jobs.cancel_reason``).

Both engines: both columns arrive nullable; every row keeps its data and
starts with NULLs, including a job already in the ``cancelled`` stage; up,
down and up again; a no-op where the table is absent or already has the
columns.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

from gdx_dispatch.models.tenant_models import Job

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/111_job_cancel_lifecycle.py"
NEW = ("cancelled_at", "cancel_reason")

J1 = uuid.UUID("33333333-3333-3333-3333-333333333331")
J2 = uuid.UUID("33333333-3333-3333-3333-333333333332")


def _seed(c, *, uuid_type: str, ids) -> None:
    """``jobs`` as it stood at 110 (the columns that matter), with an open
    job and one already cancelled by the old bare stage flip."""
    c.exec_driver_sql(
        f"CREATE TABLE jobs (id {uuid_type} PRIMARY KEY, title VARCHAR(200) NOT NULL, "
        "lifecycle_stage VARCHAR(32) NOT NULL, company_id VARCHAR(36) NOT NULL)"
    )
    c.execute(text("INSERT INTO jobs VALUES (:id, 'open', 'scheduled', 'co')"), {"id": ids(J1)})
    c.execute(text("INSERT INTO jobs VALUES (:id, 'gone', 'cancelled', 'co')"), {"id": ids(J2)})


def _load(conn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    spec = importlib.util.spec_from_file_location("m111", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _present(conn) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns("jobs")} & set(NEW)


def _rows(conn) -> list:
    return [tuple(r) for r in conn.exec_driver_sql(
        "SELECT title, lifecycle_stage, cancelled_at, cancel_reason FROM jobs ORDER BY title"
    ).all()]


SEEDED = [("gone", "cancelled", None, None), ("open", "scheduled", None, None)]


def test_it_chains_onto_110_and_is_the_only_reviser():
    source = MIGRATION.read_text()
    assert 'revision = "111_job_cancel_lifecycle"' in source
    assert 'down_revision = "110_time_entry_billed_invoice"' in source
    assert len("111_job_cancel_lifecycle") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [p.name for p in MIGRATION.parent.glob("*.py")
                if re.search(r'^down_revision = "110_time_entry_billed_invoice"', p.read_text(), re.M)]
    assert revising == ["111_job_cancel_lifecycle.py"], revising
    assert "%" not in source.replace("%%", "")


def test_the_orm_model_carries_both_columns_nullable():
    cols = Job.__table__.columns
    assert cols["cancelled_at"].nullable is True
    assert cols["cancelled_at"].type.timezone is True
    assert cols["cancel_reason"].nullable is True
    assert cols["cancel_reason"].type.length == 300


def test_sqlite_upgrade_keeps_every_row_and_round_trips(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm111.db'}", future=True)
    with eng.begin() as c:
        _seed(c, uuid_type="CHAR(32)", ids=lambda u: u.hex)  # SQLite stores a Uuid as 32 dashless hex
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert _present(c) == set(NEW)
        assert _rows(c) == SEEDED, "no backfill: a past cancel's time and reason were never recorded"
        m.upgrade()  # rerun is a no-op
        c.exec_driver_sql(
            "UPDATE jobs SET cancelled_at = '2026-10-07 12:00:00+00:00', cancel_reason = 'customer called' "
            "WHERE id = ?", (J1.hex,)
        )
        assert c.exec_driver_sql("SELECT cancel_reason FROM jobs WHERE id = ?", (J1.hex,)).scalar() == "customer called"
    with eng.begin() as c:
        m = _load(c)
        m.downgrade()
        assert _present(c) == set()
        assert c.exec_driver_sql("SELECT COUNT(*) FROM jobs").scalar() == 2
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert _present(c) == set(NEW)
    eng.dispose()


def test_sqlite_with_one_column_already_present_adds_only_the_other(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'half.db'}", future=True)
    with eng.begin() as c:
        _seed(c, uuid_type="CHAR(32)", ids=lambda u: u.hex)
        c.exec_driver_sql("ALTER TABLE jobs ADD COLUMN cancelled_at TIMESTAMP")
        _load(c).upgrade()
        assert _present(c) == set(NEW)
    eng.dispose()


def test_sqlite_without_the_table_is_a_no_op(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        m.downgrade()
        assert inspect(c).get_table_names() == []
    eng.dispose()


def test_on_a_create_all_database_upgrade_is_a_no_op(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'orm.db'}", future=True)
    from gdx_dispatch.models.tenant_models import Customer, CustomerLocation

    for tbl in (Customer, CustomerLocation, Job):
        tbl.__table__.create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        assert _present(c) == set(NEW), "create_all already builds both columns"
        _load(c).upgrade()
        assert _present(c) == set(NEW)
    eng.dispose()


_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 111 needs a real Postgres; set DATABASE_URL or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@_requires_pg
def test_postgres_round_trip():
    eng = create_engine(_URL, future=True)
    schema = "m111_test"
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"SET search_path TO {schema}")
            _seed(c, uuid_type="UUID", ids=str)
            c.commit()
            m = _load(c)
            m.upgrade()
            c.commit()
            types = dict(
                (name, (dtype, length, nullable))
                for name, dtype, length, nullable in c.exec_driver_sql(
                    "SELECT column_name, data_type, character_maximum_length, is_nullable "
                    "FROM information_schema.columns WHERE table_schema = 'm111_test' "
                    "AND table_name = 'jobs' AND column_name IN ('cancelled_at', 'cancel_reason')"
                ).all()
            )
            assert types == {
                "cancelled_at": ("timestamp with time zone", None, "YES"),
                "cancel_reason": ("character varying", 300, "YES"),
            }
            assert _rows(c) == SEEDED
            m.upgrade()
            c.exec_driver_sql(
                "UPDATE jobs SET cancelled_at = now(), cancel_reason = %s WHERE id = %s",
                ("customer called", str(J1)),
            )
            c.commit()
            # The width is real: an over-long reason is refused, not truncated.
            with pytest.raises(Exception, match="too long"):
                c.exec_driver_sql("UPDATE jobs SET cancel_reason = %s WHERE id = %s", ("x" * 301, str(J1)))
            c.rollback()
            c.exec_driver_sql(f"SET search_path TO {schema}")
            m.downgrade()
            c.commit()
            assert _present(c) == set()
            assert c.exec_driver_sql("SELECT COUNT(*) FROM jobs").scalar() == 2
            m.downgrade()
            m.upgrade()
            c.commit()
            assert _present(c) == set(NEW)
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
