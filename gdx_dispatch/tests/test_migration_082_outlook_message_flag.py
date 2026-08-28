"""Migration 082 adds ``outlook_messages.is_flagged`` on both engines, reversibly.

Two dialect paths (SQLite has no ADD COLUMN IF NOT EXISTS), so both are run
here rather than one asserted and the other assumed. The default is the
load-bearing part: an upgraded install must read every existing message as
NOT flagged, and the first sync after deploy — not a guess in DDL — writes
the real value (see ``tasks._delta_link_is_current``).
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
    / "migrations/versions/082_outlook_message_flag.py"
)
COLUMN = "is_flagged"

# The pre-082 shape, trimmed to what the migration touches.
SEED_SQLITE = """
CREATE TABLE outlook_messages (
    id TEXT PRIMARY KEY,
    subject TEXT,
    is_read BOOLEAN NOT NULL DEFAULT 0
)
"""
SEED_SYNC_SQLITE = """
CREATE TABLE outlook_folder_sync_state (
    id TEXT PRIMARY KEY,
    folder_id TEXT,
    delta_token TEXT,
    full_resync_required BOOLEAN NOT NULL DEFAULT 0
)
"""
SEED_PG = """
CREATE TABLE outlook_messages (
    id text PRIMARY KEY,
    subject text,
    is_read boolean NOT NULL DEFAULT false
)
"""
SEED_SYNC_PG = """
CREATE TABLE outlook_folder_sync_state (
    id text PRIMARY KEY,
    folder_id text,
    delta_token text,
    full_resync_required boolean NOT NULL DEFAULT false
)
"""
# Real prod deltaLink shape (2026-08-27): the $select lives INSIDE the opaque
# token; the query string carries only $deltatoken. Reproduced here so the
# resync assertion runs against the shape that actually exists.
_REAL_LINK = (
    "https://graph.microsoft.com/v1.0/me/mailFolders('AAMk')/messages/delta"
    "?$deltatoken=" + "x" * 600
)


def _load(conn):
    """Import the migration with ``op.get_bind()`` pointed at our connection."""
    spec = importlib.util.spec_from_file_location("m082", MIGRATION)
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


def _cols(conn) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns("outlook_messages")}


# ── Revision wiring ─────────────────────────────────────────────────────────

def test_it_chains_onto_the_current_head() -> None:
    """081 was taken by pay periods (#498) while this branch was cut; a
    migration pointing at 080 would have forked the chain into two heads."""
    source = MIGRATION.read_text()
    assert 'revision = "082_outlook_message_flag"' in source
    assert 'down_revision = "081_pay_period_settings"' in source


def test_no_unescaped_percent_signs() -> None:
    body = MIGRATION.read_text()
    assert "%" not in body.replace("%%", "")


# ── SQLite ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm082.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(SEED_SQLITE)
        c.exec_driver_sql(SEED_SYNC_SQLITE)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_adds_the_column(sqlite_conn):
    m = _load(sqlite_conn)
    assert COLUMN not in _cols(sqlite_conn)
    m.upgrade()
    assert COLUMN in _cols(sqlite_conn)


def test_sqlite_existing_rows_read_as_not_flagged(sqlite_conn):
    sqlite_conn.exec_driver_sql(
        "INSERT INTO outlook_messages (id, subject, is_read) VALUES ('m1', 'hi', 1)"
    )
    m = _load(sqlite_conn)
    m.upgrade()
    row = sqlite_conn.exec_driver_sql(
        "SELECT is_flagged, is_read FROM outlook_messages WHERE id = 'm1'"
    ).first()
    assert not row[0], "an upgrade must not invent a flag"
    assert row[1] == 1, "and must not touch the neighbouring column"


def test_sqlite_upgrade_forces_one_resync_per_folder(sqlite_conn):
    """Without this, every folder keeps replaying a deltaLink minted under the
    pre-flag $select and is_flagged stays False forever (the audit's finding).
    The migration must flip the same switch the 410-Gone handler uses."""
    sqlite_conn.exec_driver_sql(
        "INSERT INTO outlook_folder_sync_state (id, folder_id, delta_token, full_resync_required) "
        "VALUES ('s1', 'f1', :t, 0)", {"t": _REAL_LINK},
    )
    m = _load(sqlite_conn)
    m.upgrade()
    row = sqlite_conn.exec_driver_sql(
        "SELECT full_resync_required, delta_token FROM outlook_folder_sync_state WHERE id = 's1'"
    ).first()
    assert row[0] == 1
    assert row[1] is None


def test_sqlite_upgrade_is_rerunnable(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()
    assert COLUMN in _cols(sqlite_conn)


def test_sqlite_downgrade_removes_it_and_keeps_the_row(sqlite_conn):
    sqlite_conn.exec_driver_sql(
        "INSERT INTO outlook_messages (id, subject, is_read) VALUES ('m1', 'hi', 0)"
    )
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    assert COLUMN not in _cols(sqlite_conn)
    assert sqlite_conn.exec_driver_sql(
        "SELECT subject FROM outlook_messages WHERE id = 'm1'"
    ).first() == ("hi",)


# ── Postgres ────────────────────────────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 082 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@pytest.fixture()
def pg_conn() -> Generator[Engine, None, None]:
    eng = create_engine(_URL, future=True)
    with eng.begin() as c:
        c.exec_driver_sql("DROP TABLE IF EXISTS outlook_messages")
        c.exec_driver_sql("DROP TABLE IF EXISTS outlook_folder_sync_state")
        c.exec_driver_sql(SEED_PG)
        c.exec_driver_sql(SEED_SYNC_PG)
    with eng.begin() as c:
        yield c
    with eng.begin() as c:
        c.exec_driver_sql("DROP TABLE IF EXISTS outlook_messages")
        c.exec_driver_sql("DROP TABLE IF EXISTS outlook_folder_sync_state")
    eng.dispose()


@_requires_pg
def test_pg_upgrade_adds_not_null_false_default(pg_conn):
    m = _load(pg_conn)
    assert COLUMN not in _cols(pg_conn)
    pg_conn.exec_driver_sql(
        "INSERT INTO outlook_messages (id, subject, is_read) VALUES ('m1', 'hi', true)"
    )
    m.upgrade()
    cols = {c["name"]: c for c in inspect(pg_conn).get_columns("outlook_messages")}
    assert cols[COLUMN]["nullable"] is False
    row = pg_conn.exec_driver_sql(
        "SELECT is_flagged FROM outlook_messages WHERE id = 'm1'"
    ).first()
    assert row[0] is False


@_requires_pg
def test_pg_upgrade_forces_one_resync_per_folder(pg_conn):
    pg_conn.exec_driver_sql(
        "INSERT INTO outlook_folder_sync_state (id, folder_id, delta_token, full_resync_required) "
        "VALUES ('s1', 'f1', %(t)s, false)", {"t": _REAL_LINK},
    )
    m = _load(pg_conn)
    m.upgrade()
    row = pg_conn.exec_driver_sql(
        "SELECT full_resync_required, delta_token FROM outlook_folder_sync_state WHERE id = 's1'"
    ).first()
    assert row[0] is True
    assert row[1] is None


@_requires_pg
def test_pg_upgrade_is_rerunnable(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    m.upgrade()
    assert COLUMN in _cols(pg_conn)


@_requires_pg
def test_pg_downgrade_removes_it(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    m.downgrade()
    assert COLUMN not in _cols(pg_conn)
