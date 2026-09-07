"""Migration 091: drop `inbound_sms` — on both engines, reversibly, rerunnably,
and only when it is empty. The table held 0 rows on production and demo
(2026-09-06); the Twilio webhook, routes, model and sender leave in the same
change (see test_twilio_retired.py).
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import sys
import types
from collections.abc import Generator

import pytest
from sqlalchemy import Engine, create_engine, inspect

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/091_drop_inbound_sms.py"
TABLE = "inbound_sms"

SEED = [
    "CREATE TABLE tenants (id VARCHAR(36) PRIMARY KEY, slug VARCHAR(100))",
    "INSERT INTO tenants (id, slug) VALUES ('t1', 'gdx')",
    # The shape create_all made on prod, minus indexes — enough to prove the
    # drop and the occupied-table refusal.
    "CREATE TABLE inbound_sms (id CHAR(36) PRIMARY KEY, company_id VARCHAR(64) NOT NULL, "
    "from_number VARCHAR(30) NOT NULL, to_number VARCHAR(30) NOT NULL, body TEXT NOT NULL, "
    "provider VARCHAR(30) NOT NULL, provider_message_id VARCHAR(100), customer_id CHAR(36), "
    "job_id CHAR(36), processed_at TIMESTAMP, received_at TIMESTAMP NOT NULL, created_at TIMESTAMP NOT NULL)",
]

_OCCUPY = (
    "INSERT INTO inbound_sms (id, company_id, from_number, to_number, body, provider, received_at, created_at) "
    "VALUES ('s1', 't1', '+15550001', '+15550002', 'hi', 'twilio', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
)


def _load(conn):
    """Import the migration with `op.get_bind()` pointed at our connection."""
    spec = importlib.util.spec_from_file_location("m091", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    fake = types.ModuleType("alembic")

    class _Op:
        def get_bind(self):
            return conn

    fake.op = _Op()
    saved = sys.modules.get("alembic")
    sys.modules["alembic"] = fake
    try:
        spec.loader.exec_module(mod)
    finally:
        if saved is not None:
            sys.modules["alembic"] = saved
        else:
            sys.modules.pop("alembic", None)
    return mod


def _tables(conn) -> set[str]:
    return set(inspect(conn).get_table_names())


def _cols(conn, table: str) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns(table)}


# ── Static ───────────────────────────────────────────────────────────────────

def test_it_chains_onto_090_and_090_exists() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "091_drop_inbound_sms"' in source
    assert 'down_revision = "090_dedup_copied_bug_reports"' in source
    assert len("091_drop_inbound_sms") <= 32, "alembic_version.version_num is varchar(32)"
    declaring = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^revision = "090_dedup_copied_bug_reports"', p.read_text(), re.M)
    ]
    assert declaring == ["090_dedup_copied_bug_reports.py"], f"exactly one migration must declare 090, found {declaring}"


def test_nothing_else_revises_090() -> None:
    """Two heads would stop `alembic upgrade head` on the next deploy."""
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "090_dedup_copied_bug_reports"', p.read_text(), re.M)
    ]
    assert revising == ["091_drop_inbound_sms.py"], revising


def test_no_unescaped_percent_signs() -> None:
    body = MIGRATION.read_text()
    assert "%" not in body.replace("%%", "")


def test_no_model_declares_the_dropped_table() -> None:
    """create_all runs at every boot: the ORM must not re-create what this drops."""
    from gdx_dispatch.models.tenant_models import Base

    assert TABLE not in Base.metadata.tables


# ── SQLite ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm091.db'}", future=True)
    with eng.begin() as c:
        for stmt in SEED:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_drops_the_table_and_nothing_else(sqlite_conn):
    m = _load(sqlite_conn)
    assert TABLE in _tables(sqlite_conn)
    m.upgrade()
    assert TABLE not in _tables(sqlite_conn)
    assert "tenants" in _tables(sqlite_conn)
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM tenants").scalar() == 1


def test_sqlite_refuses_to_drop_an_occupied_table(sqlite_conn):
    sqlite_conn.exec_driver_sql(_OCCUPY)
    m = _load(sqlite_conn)
    with pytest.raises(RuntimeError, match="holds 1 row"):
        m.upgrade()
    assert TABLE in _tables(sqlite_conn)
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM inbound_sms").scalar() == 1


def test_sqlite_upgrade_is_rerunnable_and_tolerates_absence(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()
    assert TABLE not in _tables(sqlite_conn)


def test_sqlite_downgrade_recreates_the_model_shape(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    assert _cols(sqlite_conn, TABLE) == {
        "id", "company_id", "from_number", "to_number", "body", "provider",
        "provider_message_id", "customer_id", "job_id", "processed_at",
        "received_at", "created_at",
    }
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM inbound_sms").scalar() == 0
    m.downgrade()  # rerun must not abort either
    m.upgrade()    # and the round trip closes
    assert TABLE not in _tables(sqlite_conn)


def test_sqlite_fresh_install_without_the_table(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(SEED[0])
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert _tables(c) == {"tenants"}
    eng.dispose()


# ── Postgres ─────────────────────────────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 091 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


@pytest.fixture()
def pg_conn() -> Generator[Engine, None, None]:
    """Every table lives in a scratch schema — the shared CI database keeps its
    real tables."""
    schema = "m091_scratch"
    eng = create_engine(_URL, future=True)
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        c.exec_driver_sql(f"SET search_path TO {schema}")
        for stmt in SEED:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        c.exec_driver_sql(f"SET search_path TO {schema}")
        yield c
    with eng.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    eng.dispose()


@_requires_pg
def test_pg_upgrade_then_downgrade_round_trip(pg_conn):
    m = _load(pg_conn)
    assert TABLE in _tables(pg_conn)
    m.upgrade()
    assert TABLE not in _tables(pg_conn)
    m.upgrade()  # rerunnable
    m.downgrade()
    assert TABLE in _tables(pg_conn)
    assert pg_conn.exec_driver_sql("SELECT COUNT(*) FROM inbound_sms").scalar() == 0
    m.upgrade()
    assert TABLE not in _tables(pg_conn)


@_requires_pg
def test_pg_refuses_to_drop_an_occupied_table(pg_conn):
    pg_conn.exec_driver_sql(_OCCUPY)
    m = _load(pg_conn)
    with pytest.raises(RuntimeError, match="holds 1 row"):
        m.upgrade()
    assert TABLE in _tables(pg_conn)
