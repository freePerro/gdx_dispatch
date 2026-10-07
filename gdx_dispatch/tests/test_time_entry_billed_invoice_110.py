"""Migration 110: a day row records the invoice that billed it
(``time_entries.billed_invoice_id``), and the older attested labor lines are
labelled ``pricing_source='labor_attested'``.

Both engines: the column, its index and its foreign key arrive; every row
keeps its data and starts unbilled; up, down and up again; a no-op where the
table is absent or already has the column; the backfill labels exactly the
attested lines that were NULL or ``manual``, and a downgrade keeps the label.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Job, TimeEntry

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/110_time_entry_billed_invoice.py"

INV = uuid.UUID("11111111-1111-1111-1111-111111111111")
TE1 = uuid.UUID("22222222-2222-2222-2222-222222222221")
TE2 = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _seed(c, *, uuid_type: str, ids) -> None:
    """The three tables as they stood at 109, with a few rows. ``ids`` turns a
    UUID into the value this engine stores."""
    c.exec_driver_sql(f"CREATE TABLE invoices (id {uuid_type} PRIMARY KEY, invoice_number VARCHAR(40))")
    c.exec_driver_sql(
        f"CREATE TABLE time_entries (id {uuid_type} PRIMARY KEY, job_id {uuid_type}, "
        "tech_id VARCHAR(80) NOT NULL, duration_minutes INTEGER)"
    )
    c.exec_driver_sql(
        "CREATE TABLE invoice_lines (id VARCHAR(36) PRIMARY KEY, description TEXT NOT NULL, "
        "labor_source VARCHAR(16), pricing_source VARCHAR(32))"
    )
    c.execute(text("INSERT INTO invoices VALUES (:id, 'INV-1')"), {"id": ids(INV)})
    c.execute(text("INSERT INTO time_entries VALUES (:id, NULL, 't1', 480)"), {"id": ids(TE1)})
    c.execute(text("INSERT INTO time_entries VALUES (:id, NULL, 't2', 0)"), {"id": ids(TE2)})
    for row in (
        ("a_null", "attested", None),          # the autodraft's bare InvoiceLine()
        ("b_manual", "attested", "manual"),    # a picker line the create path stamped
        ("c_matrix", "matrix", "manual"),      # an install line: not attested
        ("d_none", None, None),                # a part
        ("e_override", "attested", "line_override"),  # a hand-priced line: left alone
        ("f_done", "attested", "labor_attested"),
    ):
        c.execute(
            text("INSERT INTO invoice_lines VALUES (:id, 'x', :labor, :pricing)"),
            dict(zip(("id", "labor", "pricing"), row, strict=True)),
        )


def _sqlite_ids(u: uuid.UUID) -> str:
    return u.hex  # SQLite stores a Uuid as 32 dashless hex


def _pg_ids(u: uuid.UUID) -> str:
    return str(u)


def _load(conn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    spec = importlib.util.spec_from_file_location("m110", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _has_column(conn) -> bool:
    return "billed_invoice_id" in {c["name"] for c in inspect(conn).get_columns("time_entries")}


def _has_index(conn) -> bool:
    return "ix_time_entries_billed_invoice_id" in {ix["name"] for ix in inspect(conn).get_indexes("time_entries")}


def _fk_targets(conn) -> list:
    return [(fk["referred_table"], fk["referred_columns"]) for fk in inspect(conn).get_foreign_keys("time_entries")]


def _labels(conn) -> dict:
    return dict(conn.exec_driver_sql("SELECT id, pricing_source FROM invoice_lines ORDER BY id").all())


LABELLED = {
    "a_null": "labor_attested",
    "b_manual": "labor_attested",
    "c_matrix": "manual",
    "d_none": None,
    "e_override": "line_override",
    "f_done": "labor_attested",
}


def test_it_chains_onto_109_and_is_the_only_reviser():
    source = MIGRATION.read_text()
    assert 'revision = "110_time_entry_billed_invoice"' in source
    assert 'down_revision = "109_stripe_webhook_events"' in source
    assert len("110_time_entry_billed_invoice") <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [p.name for p in MIGRATION.parent.glob("*.py")
                if re.search(r'^down_revision = "109_stripe_webhook_events"', p.read_text(), re.M)]
    assert revising == ["110_time_entry_billed_invoice.py"], revising
    assert "%" not in source.replace("%%", "")


def test_the_orm_model_carries_the_column():
    col = TimeEntry.__table__.columns["billed_invoice_id"]
    assert col.nullable is True
    assert col.index is True
    assert [fk.target_fullname for fk in col.foreign_keys] == ["invoices.id"]


def test_sqlite_upgrade_keeps_every_row_unbilled_and_round_trips(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm110.db'}", future=True)
    with eng.begin() as c:
        _seed(c, uuid_type="CHAR(32)", ids=_sqlite_ids)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert _has_column(c) and _has_index(c)
        assert ("invoices", ["id"]) in _fk_targets(c)
        assert c.exec_driver_sql(
            "SELECT id, tech_id, duration_minutes, billed_invoice_id FROM time_entries ORDER BY id"
        ).all() == [(TE1.hex, "t1", 480, None), (TE2.hex, "t2", 0, None)]
        assert _labels(c) == LABELLED
        m.upgrade()  # rerun is a no-op
        assert _labels(c) == LABELLED
        c.exec_driver_sql("UPDATE time_entries SET billed_invoice_id = ? WHERE id = ?", (INV.hex, TE1.hex))
    with eng.begin() as c:
        m = _load(c)
        m.downgrade()
        assert not _has_column(c) and not _has_index(c)
        assert c.exec_driver_sql("SELECT COUNT(*) FROM time_entries").scalar() == 2
        assert _labels(c) == LABELLED, "the downgrade keeps the provenance label"
        m.downgrade()  # rerun is a no-op
        m.upgrade()
        assert _has_column(c) and _has_index(c)
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
    for tbl in (Job, Invoice, InvoiceLine, TimeEntry):
        tbl.__table__.create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        assert _has_column(c) and _has_index(c), "create_all already builds the column"
        _load(c).upgrade()
        assert _has_column(c) and _has_index(c)
    eng.dispose()


_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 110 needs a real Postgres; set DATABASE_URL or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@_requires_pg
def test_postgres_round_trip():
    eng = create_engine(_URL, future=True)
    schema = "m110_test"
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
                "WHERE table_schema = 'm110_test' AND table_name = 'time_entries' "
                "AND column_name = 'billed_invoice_id'"
            ).one()
            assert tuple(row) == ("uuid", "YES")
            assert _has_index(c)
            assert ("invoices", ["id"]) in _fk_targets(c)
            assert [b for (b,) in c.exec_driver_sql("SELECT billed_invoice_id FROM time_entries")] == [None, None]
            assert _labels(c) == LABELLED
            m.upgrade()
            c.exec_driver_sql("UPDATE time_entries SET billed_invoice_id = %s WHERE id = %s", (str(INV), str(TE1)))
            c.commit()
            # The foreign key is real: a row cannot point at an invoice that is not there.
            with pytest.raises(Exception, match="foreign key"):
                c.exec_driver_sql(
                    "UPDATE time_entries SET billed_invoice_id = %s WHERE id = %s", (str(uuid.uuid4()), str(TE2))
                )
            c.rollback()
            c.exec_driver_sql(f"SET search_path TO {schema}")
            m.downgrade()
            c.commit()
            assert not _has_column(c)
            assert c.exec_driver_sql("SELECT COUNT(*) FROM time_entries").scalar() == 2
            assert _labels(c) == LABELLED
            m.downgrade()
            m.upgrade()
            c.commit()
            assert _has_column(c) and _has_index(c)
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
