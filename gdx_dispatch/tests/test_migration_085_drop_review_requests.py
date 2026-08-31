"""Migration 085 drops review_requests on both engines, reversibly.

The table had one writer nothing called and no reader (0 rows on prod and
demo). Both dialect paths are exercised — a drop that only works on the
engine the developer happened to run is the kind of migration that halts a
deploy at the health gate.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import types
from collections.abc import Generator

import pytest
from sqlalchemy import Engine, create_engine, inspect

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations/versions/085_drop_review_requests.py"
)

SEED = """
CREATE TABLE review_requests (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    customer_id TEXT,
    status TEXT NOT NULL,
    message TEXT,
    google_reviews_link TEXT,
    scheduled_for TEXT,
    sent_at TEXT,
    created_at TEXT
)
"""
SEED_NEIGHBOUR = "CREATE TABLE customer_reviews (id TEXT PRIMARY KEY, rating INTEGER)"


def _load(conn):
    """Import the migration with `op.get_bind()` pointed at our connection."""
    spec = importlib.util.spec_from_file_location("m085", MIGRATION)
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


def test_it_chains_onto_the_current_head() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "085_drop_review_requests"' in source
    assert 'down_revision = "084_google_review_url"' in source
    assert len("085_drop_review_requests") <= 32, "alembic_version.version_num is varchar(32)"


def test_no_unescaped_percent_signs() -> None:
    body = MIGRATION.read_text()
    assert "%" not in body.replace("%%", "")


# ── SQLite ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm085.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(SEED)
        c.exec_driver_sql(SEED_NEIGHBOUR)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_drops_only_this_table(sqlite_conn):
    m = _load(sqlite_conn)
    assert "review_requests" in _tables(sqlite_conn)
    m.upgrade()
    assert "review_requests" not in _tables(sqlite_conn)
    assert "customer_reviews" in _tables(sqlite_conn), "the real review table must survive"


def test_sqlite_upgrade_is_rerunnable_and_tolerates_absence(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()  # already gone — must not abort
    assert "review_requests" not in _tables(sqlite_conn)


def test_sqlite_downgrade_recreates_the_empty_table(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    assert "review_requests" in _tables(sqlite_conn)
    cols = {c["name"] for c in inspect(sqlite_conn).get_columns("review_requests")}
    assert cols == {"id", "job_id", "customer_id", "status", "message",
                    "google_reviews_link", "scheduled_for", "sent_at", "created_at"}
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM review_requests").scalar() == 0
    m.downgrade()  # rerun must not abort either


# ── Postgres ────────────────────────────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 085 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@pytest.fixture()
def pg_conn() -> Generator[Engine, None, None]:
    eng = create_engine(_URL, future=True)
    with eng.begin() as c:
        c.exec_driver_sql("DROP TABLE IF EXISTS review_requests")
        c.exec_driver_sql("DROP TABLE IF EXISTS customer_reviews")
        c.exec_driver_sql(SEED)
        c.exec_driver_sql(SEED_NEIGHBOUR)
    with eng.begin() as c:
        yield c
    with eng.begin() as c:
        c.exec_driver_sql("DROP TABLE IF EXISTS review_requests")
        c.exec_driver_sql("DROP TABLE IF EXISTS customer_reviews")
    eng.dispose()


@_requires_pg
def test_pg_upgrade_drops_only_this_table(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    assert "review_requests" not in _tables(pg_conn)
    assert "customer_reviews" in _tables(pg_conn)


@_requires_pg
def test_pg_upgrade_is_rerunnable(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    m.upgrade()
    assert "review_requests" not in _tables(pg_conn)


@_requires_pg
def test_pg_downgrade_recreates_the_empty_table(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    m.downgrade()
    assert "review_requests" in _tables(pg_conn)
    assert pg_conn.exec_driver_sql("SELECT COUNT(*) FROM review_requests").scalar() == 0


@_requires_pg
def test_pg_downgrade_rerun_does_not_poison_the_transaction(pg_conn):
    """Audit 2026-08-31: a try/except around CREATE TABLE swallowed the
    error but left alembic's Postgres transaction in the failed state —
    the next statement raised InFailedSqlTransaction. IF NOT EXISTS keeps
    the transaction healthy; this proves the connection is still usable."""
    m = _load(pg_conn)
    m.upgrade()
    m.downgrade()
    m.downgrade()  # table already present
    assert pg_conn.exec_driver_sql("SELECT 1").scalar() == 1
    assert "review_requests" in _tables(pg_conn)
