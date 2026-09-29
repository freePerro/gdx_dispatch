"""Migration 101: estimates.lead_id + leads.selected_estimate_id, on both
engines, rerunnable, reversible, a no-op without the tables, and the backfill
links exactly the estimate each LIVE lead started — never a deleted lead's,
never overwriting a link that is already there."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/101_estimate_lead_link.py"

LEAD_LIVE = uuid.UUID("11111111-1111-1111-1111-111111111111")
LEAD_DELETED = uuid.UUID("22222222-2222-2222-2222-222222222222")
LEAD_OTHER = uuid.UUID("33333333-3333-3333-3333-333333333333")
EST_STARTED = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
EST_OF_DELETED = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
EST_UNLINKED = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


def _load(conn):
    spec = importlib.util.spec_from_file_location("m101", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.op = Operations(MigrationContext.configure(conn))
    return mod


def _cols(conn, table):
    return {c["name"] for c in inspect(conn).get_columns(table)}


def _seed(conn, as_text):
    """as_text renders a UUID the way the engine stores sa.Uuid: SQLite keeps
    32 dashless hex (CLAUDE.md sharp edge), Postgres a native uuid."""
    ins_lead = text(
        "INSERT INTO leads (id, estimate_id, deleted_at, created_at) "
        "VALUES (:id, :est, :deleted, '2026-09-01')"
    )
    for lead, est, deleted in (
        (LEAD_LIVE, EST_STARTED, None),
        (LEAD_DELETED, EST_OF_DELETED, "2026-09-02"),
        (LEAD_OTHER, None, None),
    ):
        conn.execute(ins_lead, {
            "id": as_text(lead), "est": as_text(est) if est else None, "deleted": deleted,
        })
    for est in (EST_STARTED, EST_OF_DELETED, EST_UNLINKED):
        conn.execute(text("INSERT INTO estimates (id) VALUES (:id)"), {"id": as_text(est)})


def _lead_of(conn, est, as_text):
    return conn.execute(
        text("SELECT lead_id FROM estimates WHERE id = :id"), {"id": as_text(est)}
    ).scalar()


def test_it_chains_onto_100_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "101_estimate_lead_link"' in source
    assert 'down_revision = "100_payment_confirm_senders"' in source
    assert len("101_estimate_lead_link") <= 32
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "100_payment_confirm_senders"', p.read_text(), re.M)
    ]
    assert revising == ["101_estimate_lead_link.py"], revising


def test_no_unescaped_percent_signs() -> None:
    assert "%" not in MIGRATION.read_text().replace("%%", "")


SQLITE_DDL = [
    "CREATE TABLE leads (id CHAR(32) PRIMARY KEY, estimate_id CHAR(32), deleted_at TEXT, created_at TEXT)",
    "CREATE TABLE estimates (id CHAR(32) PRIMARY KEY)",
]


def _hex(u: uuid.UUID) -> str:
    return u.hex


def test_sqlite_upgrade_backfills_live_leads_only_rerun_and_round_trip(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm101.db'}", future=True)
    with eng.begin() as c:
        for stmt in SQLITE_DDL:
            c.exec_driver_sql(stmt)
        _seed(c, _hex)
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert "lead_id" in _cols(c, "estimates")
        assert "selected_estimate_id" in _cols(c, "leads")
        # Same representation as the source column — a dashed value here
        # would never match an ORM read on SQLite.
        assert _lead_of(c, EST_STARTED, _hex) == LEAD_LIVE.hex
        assert _lead_of(c, EST_OF_DELETED, _hex) is None
        assert _lead_of(c, EST_UNLINKED, _hex) is None
        # An existing link is never overwritten by a rerun.
        c.execute(
            text("UPDATE estimates SET lead_id = :lead WHERE id = :est"),
            {"lead": LEAD_OTHER.hex, "est": EST_STARTED.hex},
        )
        m.upgrade()
        assert _lead_of(c, EST_STARTED, _hex) == LEAD_OTHER.hex
        m.downgrade()
        assert "lead_id" not in _cols(c, "estimates")
        assert "selected_estimate_id" not in _cols(c, "leads")
        assert c.exec_driver_sql("SELECT COUNT(*) FROM estimates").scalar() == 3
        m.downgrade()
        m.upgrade()
        assert "lead_id" in _cols(c, "estimates")
    eng.dispose()


def test_sqlite_without_the_tables_is_a_no_op(tmp_path):
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
    reason="the Postgres arm of 101 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@_requires_pg
def test_postgres_round_trip_and_backfill():
    eng = create_engine(_URL, future=True)
    schema = "m101_test"
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    try:
        with eng.connect() as c:
            c.exec_driver_sql(f"SET search_path TO {schema}")
            c.exec_driver_sql(
                "CREATE TABLE leads (id uuid PRIMARY KEY, estimate_id uuid, "
                "deleted_at timestamptz, created_at timestamptz)"
            )
            c.exec_driver_sql("CREATE TABLE estimates (id uuid PRIMARY KEY)")
            _seed(c, str)
            c.commit()
            m = _load(c)
            m.upgrade()
            c.commit()
            assert "lead_id" in _cols(c, "estimates")
            assert "selected_estimate_id" in _cols(c, "leads")
            assert _lead_of(c, EST_STARTED, str) == LEAD_LIVE
            assert _lead_of(c, EST_OF_DELETED, str) is None
            m.upgrade()
            m.downgrade()
            c.commit()
            assert "lead_id" not in _cols(c, "estimates")
    finally:
        with eng.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        eng.dispose()
