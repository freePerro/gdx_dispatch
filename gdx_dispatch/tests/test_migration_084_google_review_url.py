"""Migration 084 adds app_settings.google_review_url on both engines, reversibly.

Same two-path shape as 081 (SQLite has no ADD COLUMN IF NOT EXISTS), so both
arms are exercised rather than one asserted and the other assumed.

The default is the load-bearing part: '' means "no link, render nothing".
An install that upgrades and never opens Settings → Branding must keep
sending exactly the footer it sent yesterday — a default pointing at THIS
shop's Google place would mail our review page from every other install.
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
    / "migrations/versions/084_google_review_url.py"
)

EXPECTED = {"google_review_url"}

# The pre-084 shape of the table, trimmed to what the migration touches.
SEED_SQLITE = """
CREATE TABLE app_settings (
    id TEXT PRIMARY KEY,
    company_name TEXT,
    timezone TEXT
)
"""
SEED_PG = """
CREATE TABLE app_settings (
    id text PRIMARY KEY,
    company_name text,
    timezone text
)
"""


def _load(conn):
    """Import the migration with `op.get_bind()` pointed at our connection."""
    spec = importlib.util.spec_from_file_location("m084", MIGRATION)
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
    return {c["name"] for c in inspect(conn).get_columns("app_settings")}


# ── Revision wiring ─────────────────────────────────────────────────────────

def test_it_chains_onto_the_current_head() -> None:
    """A migration whose down_revision points at the wrong parent either
    never runs or runs twice."""
    source = MIGRATION.read_text()
    assert 'revision = "084_google_review_url"' in source
    assert 'down_revision = "083_invoice_line_pricing_source"' in source


def test_no_unescaped_percent_signs() -> None:
    """A literal % in migration SQL is interpolated by the DBAPI and blows up
    at runtime on Postgres. House rule: escape it as %%."""
    body = MIGRATION.read_text()
    stripped = body.replace("%%", "")
    assert "%" not in stripped


# ── SQLite ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm084.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(SEED_SQLITE)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_adds_every_column(sqlite_conn):
    m = _load(sqlite_conn)
    assert not _cols(sqlite_conn) & EXPECTED
    m.upgrade()
    assert _cols(sqlite_conn) >= EXPECTED


def test_sqlite_defaults_do_not_change_an_existing_install(sqlite_conn):
    sqlite_conn.exec_driver_sql(
        "INSERT INTO app_settings (id, company_name, timezone) "
        "VALUES ('s1', 'Existing Shop', 'America/Chicago')"
    )
    m = _load(sqlite_conn)
    m.upgrade()
    row = sqlite_conn.exec_driver_sql(
        "SELECT google_review_url FROM app_settings WHERE id = 's1'"
    ).first()
    assert row[0] == "", "an upgrade must not invent a review link for anyone"


def test_sqlite_upgrade_is_rerunnable(sqlite_conn):
    """A stamped-then-rerun database must not abort halfway."""
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()
    assert _cols(sqlite_conn) >= EXPECTED


def test_sqlite_downgrade_removes_them(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    assert not _cols(sqlite_conn) & EXPECTED


def test_downgrade_keeps_the_row_itself(sqlite_conn):
    """Rollback un-configures pay periods. It must not destroy settings."""
    sqlite_conn.exec_driver_sql(
        "INSERT INTO app_settings (id, company_name, timezone) "
        "VALUES ('s1', 'Existing Shop', 'America/Chicago')"
    )
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    row = sqlite_conn.exec_driver_sql(
        "SELECT company_name, timezone FROM app_settings WHERE id = 's1'"
    ).first()
    assert row == ("Existing Shop", "America/Chicago")


# ── Postgres ────────────────────────────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 084 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    """A skipif that fails open is how "tested on both engines" quietly
    becomes false — the Postgres arm skips and the build stays green."""
    if os.environ.get("CI"):
        assert "postgresql" in _URL, (
            "CI is set but no postgres URL — the Postgres arm would skip."
        )


@pytest.fixture()
def pg_conn() -> Generator[Engine, None, None]:
    eng = create_engine(_URL, future=True)
    with eng.begin() as c:
        c.exec_driver_sql("DROP TABLE IF EXISTS app_settings")
        c.exec_driver_sql(SEED_PG)
    with eng.begin() as c:
        yield c
    with eng.begin() as c:
        c.exec_driver_sql("DROP TABLE IF EXISTS app_settings")
    eng.dispose()


@_requires_pg
def test_pg_upgrade_adds_every_column(pg_conn):
    m = _load(pg_conn)
    assert not _cols(pg_conn) & EXPECTED
    m.upgrade()
    assert _cols(pg_conn) >= EXPECTED


@_requires_pg
def test_pg_defaults_match_the_sqlite_arm(pg_conn):
    """Two dialect paths, one behavior. If these ever differ, a Postgres
    install and a SQLite install disagree about what a fresh row means."""
    pg_conn.exec_driver_sql(
        "INSERT INTO app_settings (id, company_name, timezone) "
        "VALUES ('s1', 'Existing Shop', 'America/Chicago')"
    )
    m = _load(pg_conn)
    m.upgrade()
    row = pg_conn.exec_driver_sql(
        "SELECT google_review_url FROM app_settings WHERE id = 's1'"
    ).first()
    assert row[0] == "", "an upgrade must not invent a review link for anyone"


@_requires_pg
def test_pg_column_is_not_null_with_default(pg_conn):
    """NOT NULL + DEFAULT '' is what lets every reader treat blank as
    "unset" without a None branch — and what keeps the ORM's
    server_default and the migration telling the same story."""
    m = _load(pg_conn)
    m.upgrade()
    cols = {c["name"]: c for c in inspect(pg_conn).get_columns("app_settings")}
    assert cols["google_review_url"]["nullable"] is False
    assert cols["google_review_url"]["type"].length == 500


@_requires_pg
def test_pg_upgrade_is_rerunnable(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    m.upgrade()
    assert _cols(pg_conn) >= EXPECTED


@_requires_pg
def test_pg_downgrade_removes_them(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    m.downgrade()
    assert not _cols(pg_conn) & EXPECTED
