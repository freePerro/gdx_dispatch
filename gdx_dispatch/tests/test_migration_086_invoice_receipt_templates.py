"""Migration 086 adds the four invoice/receipt email template columns to
tenant_settings on both engines, reversibly.

Same two-path shape as 084 (SQLite has no ADD COLUMN IF NOT EXISTS), so both
arms are exercised rather than one asserted and the other assumed.

The load-bearing part is what an upgrade does NOT do: it must not write any
text into the new columns. NULL/blank means "platform default" to every
reader, so an install that upgrades and never opens Settings keeps sending
exactly the invoice and receipt copy it sent yesterday.
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
    / "migrations/versions/086_invoice_receipt_templates.py"
)

EXPECTED = {
    "invoice_email_subject_template",
    "invoice_email_body_template",
    "receipt_email_subject_template",
    "receipt_email_body_template",
}

# The pre-086 shape of the table, trimmed to what the migration touches —
# the estimate pair is the precedent these four copy.
SEED_SQLITE = """
CREATE TABLE tenant_settings (
    tenant_id TEXT PRIMARY KEY,
    estimate_email_subject_template TEXT,
    estimate_email_body_template TEXT,
    estimate_deposit_pct INTEGER NOT NULL DEFAULT 50
)
"""
SEED_PG = """
CREATE TABLE tenant_settings (
    tenant_id uuid PRIMARY KEY,
    estimate_email_subject_template text,
    estimate_email_body_template text,
    estimate_deposit_pct integer NOT NULL DEFAULT 50
)
"""
_TID = "11111111-1111-4111-8111-111111111111"


def _load(conn):
    """Import the migration with `op.get_bind()` pointed at our connection."""
    spec = importlib.util.spec_from_file_location("m086", MIGRATION)
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
    return {c["name"] for c in inspect(conn).get_columns("tenant_settings")}


# ── Revision wiring ─────────────────────────────────────────────────────────

def test_it_chains_onto_the_current_head() -> None:
    """A migration whose down_revision points at the wrong parent either
    never runs or runs twice."""
    source = MIGRATION.read_text()
    assert 'revision = "086_invoice_receipt_templates"' in source
    assert 'down_revision = "085_drop_review_requests"' in source
    assert len("086_invoice_receipt_templates") <= 32, (
        "alembic_version.version_num is varchar(32)"
    )


def test_no_unescaped_percent_signs() -> None:
    """A literal % in migration SQL is interpolated by the DBAPI and blows up
    at runtime on Postgres. House rule: escape it as %%."""
    body = MIGRATION.read_text()
    stripped = body.replace("%%", "")
    assert "%" not in stripped


# ── SQLite ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm086.db'}", future=True)
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


def test_sqlite_upgrade_leaves_the_new_columns_null(sqlite_conn):
    """NULL = "use the platform default". Writing today's default text into
    the column would freeze the wording per install."""
    sqlite_conn.exec_driver_sql(
        "INSERT INTO tenant_settings (tenant_id, estimate_email_subject_template) "
        "VALUES ('11111111-1111-4111-8111-111111111111', 'Quote {{estimate_number}}')"
    )
    m = _load(sqlite_conn)
    m.upgrade()
    row = sqlite_conn.exec_driver_sql(
        "SELECT invoice_email_subject_template, invoice_email_body_template, "
        "receipt_email_subject_template, receipt_email_body_template, "
        "estimate_email_subject_template FROM tenant_settings"
    ).first()
    assert row[:4] == (None, None, None, None), "an upgrade must not invent template text"
    assert row[4] == "Quote {{estimate_number}}", "the estimate template must survive untouched"


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


def test_sqlite_downgrade_keeps_the_row_itself(sqlite_conn):
    """Rollback un-configures the templates. It must not destroy settings."""
    sqlite_conn.exec_driver_sql(
        "INSERT INTO tenant_settings (tenant_id, estimate_deposit_pct) "
        "VALUES ('11111111-1111-4111-8111-111111111111', 35)"
    )
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    row = sqlite_conn.exec_driver_sql(
        "SELECT tenant_id, estimate_deposit_pct FROM tenant_settings"
    ).first()
    assert row == (_TID, 35)


# ── Postgres ────────────────────────────────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 086 needs a real Postgres; set DATABASE_URL "
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
        c.exec_driver_sql("DROP TABLE IF EXISTS tenant_settings")
        c.exec_driver_sql(SEED_PG)
    with eng.begin() as c:
        yield c
    with eng.begin() as c:
        c.exec_driver_sql("DROP TABLE IF EXISTS tenant_settings")
    eng.dispose()


@_requires_pg
def test_pg_upgrade_adds_every_column(pg_conn):
    m = _load(pg_conn)
    assert not _cols(pg_conn) & EXPECTED
    m.upgrade()
    assert _cols(pg_conn) >= EXPECTED


@_requires_pg
def test_pg_new_columns_are_nullable_text_left_null(pg_conn):
    """Two dialect paths, one behavior: nullable text, nothing written."""
    pg_conn.exec_driver_sql(
        "INSERT INTO tenant_settings (tenant_id) VALUES ('11111111-1111-4111-8111-111111111111')"
    )
    m = _load(pg_conn)
    m.upgrade()
    cols = {c["name"]: c for c in inspect(pg_conn).get_columns("tenant_settings")}
    for name in EXPECTED:
        assert cols[name]["nullable"] is True, name
        assert str(cols[name]["type"]).upper() == "TEXT", name
    row = pg_conn.exec_driver_sql(
        "SELECT invoice_email_subject_template, invoice_email_body_template, "
        "receipt_email_subject_template, receipt_email_body_template FROM tenant_settings"
    ).first()
    assert row == (None, None, None, None)


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
