"""Migration 081 adds the pay-period columns, on both engines, reversibly.

Every migration in this repo must run on SQLite AND Postgres, and 081 takes
two different code paths to do it (SQLite has no ADD COLUMN IF NOT EXISTS).
Two paths is two chances to drift, so both are exercised here rather than
one being asserted and the other assumed.

The defaults are the load-bearing part. An install that upgrades into this
feature and never opens the setting must keep the view it had yesterday
(`weekly_mon`, matching what both timesheet screens already assumed) and
must NOT start emailing anybody's hours (`payroll_autosend_enabled` false).
A default of this shop's own biweekly, or an autosend defaulting on, would
ship a behavior change to every other install as a side effect of a schema
change.
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
    / "migrations/versions/081_pay_period_settings.py"
)

EXPECTED = {
    "pay_period_cadence",
    "pay_period_anchor_start",
    "pay_period_pay_lag_days",
    "payroll_recipient_emails",
    "payroll_autosend_enabled",
    "payroll_autosend_hour",
}

# The pre-081 shape of the table, trimmed to what the migration touches.
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
    spec = importlib.util.spec_from_file_location("m081", MIGRATION)
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
    assert 'revision = "081_pay_period_settings"' in source
    assert 'down_revision = "080_adjustment_tax_component"' in source


def test_no_unescaped_percent_signs() -> None:
    """A literal % in migration SQL is interpolated by the DBAPI and blows up
    at runtime on Postgres. House rule: escape it as %%."""
    body = MIGRATION.read_text()
    stripped = body.replace("%%", "")
    assert "%" not in stripped


# ── SQLite ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm081.db'}", future=True)
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
        "SELECT pay_period_cadence, pay_period_pay_lag_days, "
        "payroll_recipient_emails, payroll_autosend_enabled "
        "FROM app_settings WHERE id = 's1'"
    ).first()
    assert row[0] == "weekly_mon", "an upgrade must not move anyone's week"
    assert row[1] == 0
    assert row[2] == ""
    assert not row[3], "an upgrade must not start emailing hours"


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
    reason="the Postgres arm of 081 needs a real Postgres; set DATABASE_URL "
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
        "SELECT pay_period_cadence, pay_period_pay_lag_days, "
        "payroll_recipient_emails, payroll_autosend_enabled, payroll_autosend_hour "
        "FROM app_settings WHERE id = 's1'"
    ).first()
    assert row[0] == "weekly_mon"
    assert row[1] == 0
    assert row[2] == ""
    assert row[3] is False
    assert row[4] == 7


@_requires_pg
def test_pg_columns_are_not_null_with_defaults(pg_conn):
    """A NULL cadence would fall through to the module default anyway, but a
    NOT NULL column with a default is what makes that fallback unreachable
    in practice rather than merely unlikely."""
    m = _load(pg_conn)
    m.upgrade()
    cols = {c["name"]: c for c in inspect(pg_conn).get_columns("app_settings")}
    for name in EXPECTED - {"pay_period_anchor_start"}:
        assert cols[name]["nullable"] is False, f"{name} should be NOT NULL"
    assert cols["pay_period_anchor_start"]["nullable"] is True


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
