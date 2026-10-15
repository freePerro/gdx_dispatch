"""Migration 127: a closeout records whether it restates a still-completed job,
and what its claim billed (``job_closeouts.is_restatement`` and
``man_hours_billed``, GDXA-890).

Both engines: both columns arrive nullable; every row keeps its data and reads
NULL (not a restatement; a pre-127 claim billed its full hours); up, down and up again; a no-op where the table is
absent or already has the column. On SQLite the batch rebuild must keep
``uq_job_closeouts_live`` partial.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Invoice, Job, JobCloseout

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/127_closeout_is_restatement.py"

JOB = uuid.UUID("33333333-3333-3333-3333-333333333333")
CO_OLD = uuid.UUID("22222222-2222-2222-2222-222222222221")
CO_LIVE = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _seed(c, *, uuid_type: str, ids) -> None:
    """``job_closeouts`` as it stood at 120 (the columns this test reads)."""
    c.exec_driver_sql(
        f"CREATE TABLE job_closeouts (id {uuid_type} PRIMARY KEY, job_id {uuid_type} NOT NULL, "
        f"hours_worked NUMERIC(8, 2) NOT NULL, supersedes_id {uuid_type}, "
        "superseded_at TIMESTAMP, deleted_at TIMESTAMP)"
    )
    c.exec_driver_sql(
        "CREATE UNIQUE INDEX uq_job_closeouts_live ON job_closeouts (job_id) "
        "WHERE superseded_at IS NULL AND deleted_at IS NULL"
    )
    c.execute(
        text("INSERT INTO job_closeouts VALUES (:id, :job, 2.00, NULL, '2026-10-01 17:00:00', NULL)"),
        {"id": ids(CO_OLD), "job": ids(JOB)},
    )
    c.execute(
        text("INSERT INTO job_closeouts VALUES (:id, :job, 3.00, :old, NULL, NULL)"),
        {"id": ids(CO_LIVE), "job": ids(JOB), "old": ids(CO_OLD)},
    )


def _sqlite_ids(u: uuid.UUID) -> str:
    return u.hex  # SQLite stores a Uuid as 32 dashless hex


def _pg_ids(u: uuid.UUID) -> str:
    return str(u)


def _load(conn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    spec = importlib.util.spec_from_file_location("m127", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


_NEW = {"is_restatement", "man_hours_billed"}


def _has_column(conn) -> bool:
    """Both 127 columns are there (and asserts it is never just one)."""
    present = _NEW & {c["name"] for c in inspect(conn).get_columns("job_closeouts")}
    assert present in (set(), _NEW), present
    return present == _NEW


def _assert_live_index_is_partial(c, ids) -> None:
    sp = c.begin_nested()
    with pytest.raises(IntegrityError):
        c.execute(
            text("INSERT INTO job_closeouts (id, job_id, hours_worked) VALUES (:id, :job, 1)"),
            {"id": ids(uuid.uuid4()), "job": ids(JOB)},
        )
    sp.rollback()
    sp = c.begin_nested()
    c.execute(
        text("INSERT INTO job_closeouts (id, job_id, hours_worked, superseded_at) "
             "VALUES (:id, :job, 1, '2026-10-02 17:00:00')"),
        {"id": ids(uuid.uuid4()), "job": ids(JOB)},
    )
    sp.rollback()


def test_it_chains_onto_126_and_is_the_only_reviser():
    source = MIGRATION.read_text()
    assert 'revision = "127_closeout_is_restatement"' in source
    assert 'down_revision = "126_completed_job_visits_closed"' in source
    assert len("127_closeout_is_restatement") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [p.name for p in MIGRATION.parent.glob("*.py")
                if re.search(r'^down_revision = "126_completed_job_visits_closed"', p.read_text(), re.M)]
    assert revising == ["127_closeout_is_restatement.py"], revising
    assert "%" not in source.replace("%%", "")


def test_the_orm_model_carries_the_column():
    assert JobCloseout.__table__.columns["is_restatement"].nullable is True
    assert JobCloseout.__table__.columns["man_hours_billed"].nullable is True


def test_sqlite_upgrade_keeps_every_row_and_round_trips(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm127.db'}", future=True)
    with eng.begin() as c:
        _seed(c, uuid_type="CHAR(32)", ids=_sqlite_ids)
    select = "SELECT id, job_id, hours_worked, supersedes_id, superseded_at FROM job_closeouts ORDER BY id"
    with eng.begin() as c:
        rows_before = c.exec_driver_sql(select).all()
        m = _load(c)
        m.upgrade()
        assert _has_column(c)
        assert c.exec_driver_sql(select).all() == rows_before
        assert c.exec_driver_sql("SELECT is_restatement, man_hours_billed FROM job_closeouts").all() == [
            (None, None), (None, None)]
        _assert_live_index_is_partial(c, _sqlite_ids)
        m.upgrade()  # rerun is a no-op
        c.exec_driver_sql("UPDATE job_closeouts SET is_restatement = 1 WHERE id = ?", (CO_LIVE.hex,))
    with eng.begin() as c:
        m = _load(c)
        m.downgrade()
        assert not _has_column(c)
        assert c.exec_driver_sql(select).all() == rows_before
        _assert_live_index_is_partial(c, _sqlite_ids)
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert _has_column(c)
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
    TenantBase.metadata.create_all(bind=eng)
    for tbl in (Job, Invoice, JobCloseout):
        tbl.__table__.create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        assert _has_column(c), "create_all already builds the column"
        _load(c).upgrade()
        assert _has_column(c)
    eng.dispose()


_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 127 needs a real Postgres; set DATABASE_URL or TEST_DATABASE_URL to a postgres url",
)
def test_postgres_round_trip():
    eng = create_engine(_URL, future=True)
    schema = "m127_test"
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"SET search_path TO {schema}")
            _seed(c, uuid_type="UUID", ids=_pg_ids)
            c.commit()
            m = _load(c)
            m.upgrade()
            c.commit()
            rows = c.exec_driver_sql(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'm127_test' AND table_name = 'job_closeouts' "
                "AND column_name IN ('is_restatement', 'man_hours_billed') ORDER BY column_name"
            ).all()
            assert [tuple(r) for r in rows] == [("is_restatement", "boolean", "YES"),
                                                ("man_hours_billed", "numeric", "YES")]
            assert [r for (r,) in c.exec_driver_sql("SELECT is_restatement FROM job_closeouts")] == [None, None]
            _assert_live_index_is_partial(c, _pg_ids)
            m.upgrade()
            m.downgrade()
            c.commit()
            assert not _has_column(c)
            assert c.exec_driver_sql("SELECT COUNT(*) FROM job_closeouts").scalar() == 2
            m.downgrade()
            m.upgrade()
            c.commit()
            assert _has_column(c)
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
