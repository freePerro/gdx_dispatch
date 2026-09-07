"""Migration 092: drop the five tables behind routes that never served —
on both engines, reversibly, rerunnably, all-or-nothing, and only when every
one of them is empty. All five held 0 rows on production and demo
(2026-09-06); the routers and models leave in the same change
(see test_dead_duplicates_retired.py).
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

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions/092_drop_dead_duplicate_tables.py"
TABLES = ("po_request_lines", "po_requests", "booking_jobs_router", "booking_requests_router", "portal_booking_requests")

SEED = [
    "CREATE TABLE tenants (id VARCHAR(36) PRIMARY KEY, slug VARCHAR(100))",
    "INSERT INTO tenants (id, slug) VALUES ('t1', 'gdx')",
    # The shapes create_all made on prod, minus indexes — enough to prove the
    # drop and the occupied-table refusal.
    "CREATE TABLE po_request_lines (id VARCHAR(36) PRIMARY KEY, po_id VARCHAR(36) NOT NULL, sku VARCHAR(100), "
    "name VARCHAR(300) NOT NULL, quantity INTEGER NOT NULL, unit_price NUMERIC(12, 2) NOT NULL)",
    "CREATE TABLE po_requests (id VARCHAR(36) PRIMARY KEY, company_id VARCHAR(36) NOT NULL, requested_by VARCHAR(36) NOT NULL, "
    "job_id VARCHAR(36), customer_id VARCHAR(36), supplier_name VARCHAR(300), status VARCHAR(30) NOT NULL, notes TEXT, "
    "created_at TIMESTAMP, approved_at TIMESTAMP, received_at TIMESTAMP, deleted_at TIMESTAMP)",
    "CREATE TABLE booking_jobs_router (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, booking_request_id TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE booking_requests_router (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL, "
    "service TEXT NOT NULL, preferred_date TEXT NOT NULL, preferred_slot TEXT, status TEXT NOT NULL, decline_reason TEXT, "
    "approved_job_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE portal_booking_requests (id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, requested_date TEXT NOT NULL, "
    "service_type TEXT NOT NULL, notes TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL)",
]

_OCCUPY = {
    "po_request_lines": "INSERT INTO po_request_lines (id, po_id, name, quantity, unit_price) VALUES ('l1', 'p1', 'spring', 1, 10.00)",
    "po_requests": "INSERT INTO po_requests (id, company_id, requested_by, status) VALUES ('p1', 't1', 'u1', 'requested')",
    "booking_jobs_router": "INSERT INTO booking_jobs_router (id, tenant_id, booking_request_id, created_at) VALUES ('bj1', 't1', 'b1', '2026-09-06')",
    "booking_requests_router": "INSERT INTO booking_requests_router (id, tenant_id, name, phone, service, preferred_date, status, created_at, updated_at) "
    "VALUES ('b1', 't1', 'Pat', '5550000000', 'repair', '2026-09-07', 'pending', '2026-09-06', '2026-09-06')",
    "portal_booking_requests": "INSERT INTO portal_booking_requests (id, customer_id, requested_date, service_type, status, created_at) "
    "VALUES ('pb1', 'c1', '2026-09-07', 'maintenance', 'requested', '2026-09-06')",
}

EXPECTED_COLUMNS = {
    "po_request_lines": {"id", "po_id", "sku", "name", "quantity", "unit_price"},
    "po_requests": {"id", "company_id", "requested_by", "job_id", "customer_id", "supplier_name", "status", "notes",
                    "created_at", "approved_at", "received_at", "deleted_at"},
    "booking_jobs_router": {"id", "tenant_id", "booking_request_id", "created_at"},
    "booking_requests_router": {"id", "tenant_id", "name", "phone", "service", "preferred_date", "preferred_slot", "status",
                                "decline_reason", "approved_job_id", "created_at", "updated_at"},
    "portal_booking_requests": {"id", "customer_id", "requested_date", "service_type", "notes", "status", "created_at"},
}


def _load(conn):
    """Import the migration with `op.get_bind()` pointed at our connection."""
    spec = importlib.util.spec_from_file_location("m092", MIGRATION)
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

def test_it_chains_onto_091_and_091_exists() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "092_drop_dead_duplicate_tables"' in source
    assert 'down_revision = "091_drop_inbound_sms"' in source
    assert len("092_drop_dead_duplicate_tables") <= 32, "alembic_version.version_num is varchar(32)"
    declaring = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^revision = "091_drop_inbound_sms"', p.read_text(), re.M)
    ]
    assert declaring == ["091_drop_inbound_sms.py"], f"exactly one migration must declare 091, found {declaring}"


def test_nothing_else_revises_091() -> None:
    """Two heads would stop `alembic upgrade head` on the next deploy."""
    revising = [
        p.name for p in MIGRATION.parent.glob("*.py")
        if re.search(r'^down_revision = "091_drop_inbound_sms"', p.read_text(), re.M)
    ]
    assert revising == ["092_drop_dead_duplicate_tables.py"], revising


def test_no_unescaped_percent_signs() -> None:
    body = MIGRATION.read_text()
    assert "%" not in body.replace("%%", "")


def test_no_model_declares_a_dropped_table() -> None:
    """create_all runs at every boot: the ORM must not re-create what this drops."""
    from gdx_dispatch.models.tenant_models import Base

    still_declared = [t for t in TABLES if t in Base.metadata.tables]
    assert not still_declared, still_declared


def test_recreate_covers_every_table_it_drops() -> None:
    m = _load(None)
    assert set(m.TABLES) == set(TABLES)
    assert set(m._RECREATE) == set(TABLES)


# ── SQLite ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm092.db'}", future=True)
    with eng.begin() as c:
        for stmt in SEED:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_drops_all_five_and_nothing_else(sqlite_conn):
    m = _load(sqlite_conn)
    assert set(TABLES) <= _tables(sqlite_conn)
    m.upgrade()
    assert _tables(sqlite_conn) == {"tenants"}
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM tenants").scalar() == 1


@pytest.mark.parametrize("occupied", TABLES)
def test_sqlite_one_occupied_table_refuses_the_whole_drop(sqlite_conn, occupied):
    sqlite_conn.exec_driver_sql(_OCCUPY[occupied])
    m = _load(sqlite_conn)
    with pytest.raises(RuntimeError, match=rf"{occupied} holds 1 row"):
        m.upgrade()
    # All-or-nothing: the four empty siblings survive too.
    assert set(TABLES) <= _tables(sqlite_conn)
    assert sqlite_conn.exec_driver_sql(f"SELECT COUNT(*) FROM {occupied}").scalar() == 1  # noqa: S608 — test constant


def test_sqlite_upgrade_is_rerunnable_and_tolerates_absence(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()
    assert _tables(sqlite_conn) == {"tenants"}


def test_sqlite_upgrade_tolerates_a_partial_set(sqlite_conn):
    """A fresh install never had some of these; a half-migrated one may."""
    sqlite_conn.exec_driver_sql("DROP TABLE booking_jobs_router")
    sqlite_conn.exec_driver_sql("DROP TABLE po_requests")
    m = _load(sqlite_conn)
    m.upgrade()
    assert _tables(sqlite_conn) == {"tenants"}


def test_sqlite_downgrade_recreates_the_model_shapes(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    for t in TABLES:
        assert _cols(sqlite_conn, t) == EXPECTED_COLUMNS[t], t
        assert sqlite_conn.exec_driver_sql(f"SELECT COUNT(*) FROM {t}").scalar() == 0  # noqa: S608 — test constant
    m.downgrade()  # rerun must not abort either
    m.upgrade()    # and the round trip closes
    assert _tables(sqlite_conn) == {"tenants"}


def test_sqlite_fresh_install_without_the_tables(tmp_path):
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
    reason="the Postgres arm of 092 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


@pytest.fixture()
def pg_conn() -> Generator[Engine, None, None]:
    """Every table lives in a scratch schema — the shared CI database keeps its
    real tables."""
    schema = "m092_scratch"
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
    assert set(TABLES) <= _tables(pg_conn)
    m.upgrade()
    assert _tables(pg_conn) == {"tenants"}
    m.upgrade()  # rerunnable
    m.downgrade()
    for t in TABLES:
        assert _cols(pg_conn, t) == EXPECTED_COLUMNS[t], t
        assert pg_conn.exec_driver_sql(f"SELECT COUNT(*) FROM {t}").scalar() == 0  # noqa: S608 — test constant
    m.upgrade()
    assert _tables(pg_conn) == {"tenants"}


@_requires_pg
def test_pg_one_occupied_table_refuses_the_whole_drop(pg_conn):
    pg_conn.exec_driver_sql(_OCCUPY["portal_booking_requests"])
    m = _load(pg_conn)
    with pytest.raises(RuntimeError, match="portal_booking_requests holds 1 row"):
        m.upgrade()
    assert set(TABLES) <= _tables(pg_conn)
