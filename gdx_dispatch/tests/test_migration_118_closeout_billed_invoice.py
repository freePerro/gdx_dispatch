"""Migration 118: a closeout records the invoice that billed its final-day
hours (``job_closeouts.billed_invoice_id``, GDXA-798).

Both engines: the column, its index and its foreign key arrive; every row
keeps its data and starts unbilled; up, down and up again; a no-op where the
table is absent or already has the column. On SQLite the batch rebuild must
keep ``uq_job_closeouts_live`` partial: a second live closeout for a job is
still refused, and a superseded one is still allowed, in both directions.
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

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/118_closeout_billed_invoice.py"

INV = uuid.UUID("11111111-1111-1111-1111-111111111111")
JOB = uuid.UUID("33333333-3333-3333-3333-333333333333")
CO_OLD = uuid.UUID("22222222-2222-2222-2222-222222222221")
CO_LIVE = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _seed(c, *, uuid_type: str, ids) -> None:
    """``invoices`` and ``job_closeouts`` as they stood at 117 (the columns
    this test reads), with one superseded and one live closeout on a job."""
    c.exec_driver_sql(f"CREATE TABLE invoices (id {uuid_type} PRIMARY KEY, invoice_number VARCHAR(40))")
    c.exec_driver_sql(
        f"CREATE TABLE job_closeouts (id {uuid_type} PRIMARY KEY, job_id {uuid_type} NOT NULL, "
        "hours_worked NUMERIC(8, 2) NOT NULL, techs_on_site INTEGER NOT NULL DEFAULT 1, "
        "superseded_at TIMESTAMP, deleted_at TIMESTAMP)"
    )
    c.exec_driver_sql("CREATE INDEX ix_job_closeouts_job_id ON job_closeouts (job_id)")
    # As 041 creates it.
    c.exec_driver_sql(
        "CREATE UNIQUE INDEX uq_job_closeouts_live ON job_closeouts (job_id) "
        "WHERE superseded_at IS NULL AND deleted_at IS NULL"
    )
    c.execute(text("INSERT INTO invoices VALUES (:id, 'INV-1')"), {"id": ids(INV)})
    c.execute(
        text("INSERT INTO job_closeouts VALUES (:id, :job, 2.00, 1, '2026-10-01 17:00:00', NULL)"),
        {"id": ids(CO_OLD), "job": ids(JOB)},
    )
    c.execute(
        text("INSERT INTO job_closeouts VALUES (:id, :job, 3.50, 2, NULL, NULL)"),
        {"id": ids(CO_LIVE), "job": ids(JOB)},
    )


def _sqlite_ids(u: uuid.UUID) -> str:
    return u.hex  # SQLite stores a Uuid as 32 dashless hex


def _pg_ids(u: uuid.UUID) -> str:
    return str(u)


def _load(conn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    spec = importlib.util.spec_from_file_location("m118", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _has_column(conn) -> bool:
    return "billed_invoice_id" in {c["name"] for c in inspect(conn).get_columns("job_closeouts")}


def _has_index(conn) -> bool:
    return "ix_job_closeouts_billed_invoice_id" in {ix["name"] for ix in inspect(conn).get_indexes("job_closeouts")}


def _fk_targets(conn) -> list:
    return [(fk["referred_table"], fk["referred_columns"]) for fk in inspect(conn).get_foreign_keys("job_closeouts")]


def _assert_live_index_is_partial(c, ids) -> None:
    """A second live row for the job is refused; a superseded one is not."""
    sp = c.begin_nested()
    with pytest.raises(IntegrityError):
        c.execute(
            text("INSERT INTO job_closeouts (id, job_id, hours_worked, techs_on_site) VALUES (:id, :job, 1, 1)"),
            {"id": ids(uuid.uuid4()), "job": ids(JOB)},
        )
    sp.rollback()
    sp = c.begin_nested()
    c.execute(
        text(
            "INSERT INTO job_closeouts (id, job_id, hours_worked, techs_on_site, superseded_at) "
            "VALUES (:id, :job, 1, 1, '2026-10-02 17:00:00')"
        ),
        {"id": ids(uuid.uuid4()), "job": ids(JOB)},
    )
    sp.rollback()


def test_it_chains_onto_117_and_is_the_only_reviser():
    source = MIGRATION.read_text()
    assert 'revision = "118_closeout_billed_invoice"' in source
    assert 'down_revision = "117_trade_estimate_tax_rate_zero"' in source
    assert len("118_closeout_billed_invoice") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [p.name for p in MIGRATION.parent.glob("*.py")
                if re.search(r'^down_revision = "117_trade_estimate_tax_rate_zero"', p.read_text(), re.M)]
    assert revising == ["118_closeout_billed_invoice.py"], revising
    assert "%" not in source.replace("%%", "")


def test_the_orm_model_carries_the_column():
    col = JobCloseout.__table__.columns["billed_invoice_id"]
    assert col.nullable is True
    assert col.index is True
    assert [fk.target_fullname for fk in col.foreign_keys] == ["invoices.id"]


def test_sqlite_upgrade_keeps_every_row_unbilled_and_round_trips(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm118.db'}", future=True)
    with eng.begin() as c:
        _seed(c, uuid_type="CHAR(32)", ids=_sqlite_ids)
    rows_before = [
        (CO_OLD.hex, JOB.hex, 2, 1, "2026-10-01 17:00:00"),
        (CO_LIVE.hex, JOB.hex, 3.5, 2, None),
    ]
    select = "SELECT id, job_id, hours_worked, techs_on_site, superseded_at FROM job_closeouts ORDER BY id"
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert _has_column(c) and _has_index(c)
        assert ("invoices", ["id"]) in _fk_targets(c)
        assert c.exec_driver_sql(select).all() == rows_before
        assert [b for (b,) in c.exec_driver_sql("SELECT billed_invoice_id FROM job_closeouts")] == [None, None]
        _assert_live_index_is_partial(c, _sqlite_ids)
        m.upgrade()  # rerun is a no-op
        c.exec_driver_sql("UPDATE job_closeouts SET billed_invoice_id = ? WHERE id = ?", (INV.hex, CO_LIVE.hex))
    with eng.begin() as c:
        m = _load(c)
        m.downgrade()
        assert not _has_column(c) and not _has_index(c)
        assert c.exec_driver_sql(select).all() == rows_before
        _assert_live_index_is_partial(c, _sqlite_ids)
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert _has_column(c) and _has_index(c)
        _assert_live_index_is_partial(c, _sqlite_ids)
    eng.dispose()


def test_sqlite_without_the_tables_is_a_no_op(tmp_path):
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
        assert _has_column(c) and _has_index(c), "create_all already builds the column"
        _load(c).upgrade()
        assert _has_column(c) and _has_index(c)
    eng.dispose()


_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 118 needs a real Postgres; set DATABASE_URL or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@_requires_pg
def test_postgres_round_trip():
    eng = create_engine(_URL, future=True)
    schema = "m118_test"
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
            row = c.exec_driver_sql(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'm118_test' AND table_name = 'job_closeouts' "
                "AND column_name = 'billed_invoice_id'"
            ).one()
            assert tuple(row) == ("uuid", "YES")
            assert _has_index(c)
            assert ("invoices", ["id"]) in _fk_targets(c)
            assert [b for (b,) in c.exec_driver_sql("SELECT billed_invoice_id FROM job_closeouts")] == [None, None]
            _assert_live_index_is_partial(c, _pg_ids)
            m.upgrade()
            c.exec_driver_sql(
                "UPDATE job_closeouts SET billed_invoice_id = %s WHERE id = %s", (str(INV), str(CO_LIVE))
            )
            c.commit()
            # The foreign key is real: a closeout cannot point at an invoice that is not there.
            with pytest.raises(Exception, match="foreign key"):
                c.exec_driver_sql(
                    "UPDATE job_closeouts SET billed_invoice_id = %s WHERE id = %s",
                    (str(uuid.uuid4()), str(CO_OLD)),
                )
            c.rollback()
            c.exec_driver_sql(f"SET search_path TO {schema}")
            m.downgrade()
            c.commit()
            assert not _has_column(c)
            assert c.exec_driver_sql("SELECT COUNT(*) FROM job_closeouts").scalar() == 2
            _assert_live_index_is_partial(c, _pg_ids)
            m.downgrade()
            m.upgrade()
            c.commit()
            assert _has_column(c) and _has_index(c)
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
