"""Migration 087: copy the bug reports somewhere visible, then drop the four
retired tables and the two dead `tenants` columns — on both engines,
reversibly, rerunnably.

The copy is the part that matters: prod holds seven real bug reports in a
table no screen ever read. After the migration they are `category='bug'`
tickets on the Feedback page. A drop-only migration would have deleted them.
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
    / "migrations/versions/087_drop_retired_tables.py"
)

SEED = [
    """CREATE TABLE tenants (
        id VARCHAR(36) PRIMARY KEY, slug VARCHAR(100), name VARCHAR(255),
        subscription_status VARCHAR(32) NOT NULL DEFAULT 'trialing',
        stripe_connect_account_id VARCHAR(255), timezone VARCHAR(60))""",
    "CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, email VARCHAR(255))",
    """CREATE TABLE support_tickets (
        id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL,
        opened_by_email VARCHAR(200) NOT NULL, opened_by_user_id VARCHAR(36),
        subject VARCHAR(200) NOT NULL, body TEXT NOT NULL, category VARCHAR(20) NOT NULL,
        priority VARCHAR(20) NOT NULL, status VARCHAR(20) NOT NULL,
        created_at TIMESTAMP NOT NULL, closed_at TIMESTAMP, resolution_summary TEXT)""",
    """CREATE TABLE bug_reports (
        id VARCHAR(36) PRIMARY KEY, company_id VARCHAR(36) NOT NULL, user_id VARCHAR(36),
        subject VARCHAR(200) NOT NULL, description TEXT NOT NULL, priority VARCHAR(20),
        page_url TEXT, browser_info TEXT, status VARCHAR(20), created_at TIMESTAMP,
        resolved_at TIMESTAMP, resolved_by VARCHAR(36), resolution_notes TEXT)""",
    "CREATE TABLE tenant_module_grants (id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT)",
    "CREATE TABLE service_accounts (id TEXT PRIMARY KEY, name TEXT)",
    "CREATE TABLE platform_feature_flags (id TEXT PRIMARY KEY, key TEXT)",
    "INSERT INTO tenants (id, slug, name) VALUES ('t1', 'gdx', 'Example')",
    "INSERT INTO users (id, email) VALUES ('11111111-1111-1111-1111-111111111111', 'tech@example.com')",
    """INSERT INTO bug_reports (id, company_id, user_id, subject, description, priority, page_url, browser_info, status, created_at)
       VALUES ('r1', 't1', '11111111-1111-1111-1111-111111111111', 'No customers list on mobile', 'The list is empty', 'medium', '/mobile/customers', 'Chrome', 'new', '2026-07-22 10:00:00')""",
    """INSERT INTO bug_reports (id, company_id, user_id, subject, description, priority, status, created_at)
       VALUES ('r2', 't1', 'gone', 'Job close out', 'Will not let me close out', 'critical', 'new', '2026-08-06 09:00:00')""",
    # a report with no timestamp — the copy must still satisfy created_at NOT NULL
    """INSERT INTO bug_reports (id, company_id, user_id, subject, description, status)
       VALUES ('r3', 't1', NULL, 'No timestamp', 'Old row', 'resolved')""",
    # one report already copied (same id) — the guard must skip it
    """INSERT INTO support_tickets (id, tenant_id, opened_by_email, subject, body, category, priority, status, created_at)
       VALUES ('r2', 't1', 'tech@example.com', 'Job close out', 'already here', 'bug', 'urgent', 'open', '2026-08-06 09:00:00')""",
]
NEIGHBOUR_TABLES = {"tenants", "users", "support_tickets"}
DROPPED = {"bug_reports", "tenant_module_grants", "service_accounts", "platform_feature_flags"}


def _load(conn):
    """Import the migration with `op.get_bind()` pointed at our connection."""
    spec = importlib.util.spec_from_file_location("m087", MIGRATION)
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


def _assert_upgraded(conn) -> None:
    assert DROPPED.isdisjoint(_tables(conn))
    assert _tables(conn) >= NEIGHBOUR_TABLES
    assert {"subscription_status", "stripe_connect_account_id"}.isdisjoint(_cols(conn, "tenants"))
    rows = conn.exec_driver_sql(
        "SELECT id, opened_by_email, category, priority, status, body FROM support_tickets ORDER BY id"
    ).fetchall()
    assert [r[0] for r in rows] == ["r1", "r2", "r3"]
    r1 = rows[0]
    assert r1[1] == "tech@example.com" and r1[2] == "bug" and r1[3] == "medium" and r1[4] == "open"
    assert "The list is empty" in r1[5] and "Page: /mobile/customers" in r1[5] and "Browser: Chrome" in r1[5]
    assert rows[1][5] == "already here", "a report already in support_tickets must not be overwritten"
    assert rows[2][1] == "unknown@bug-reports" and rows[2][4] == "resolved"
    assert "copied from the retired bug_reports table" in rows[0][5]
    assert all(r[0] for r in conn.exec_driver_sql("SELECT created_at FROM support_tickets").fetchall())


def test_it_chains_onto_the_current_head() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "087_drop_retired_tables"' in source
    assert 'down_revision = "086_invoice_receipt_templates"' in source
    assert len("087_drop_retired_tables") <= 32, "alembic_version.version_num is varchar(32)"


def test_no_unescaped_percent_signs() -> None:
    body = MIGRATION.read_text()
    assert "%" not in body.replace("%%", "")


# ── SQLite ──────────────────────────────────────

@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm087.db'}", future=True)
    with eng.begin() as c:
        for stmt in SEED:
            c.exec_driver_sql(stmt)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_upgrade_copies_then_drops(sqlite_conn):
    m = _load(sqlite_conn)
    assert _tables(sqlite_conn) >= DROPPED
    m.upgrade()
    _assert_upgraded(sqlite_conn)


def test_sqlite_upgrade_is_rerunnable_and_tolerates_absence(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.upgrade()  # tables and columns already gone — must not abort
    _assert_upgraded(sqlite_conn)
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM support_tickets").scalar() == 3


def test_sqlite_refuses_to_drop_reports_it_cannot_copy(tmp_path):
    """bug_reports has rows but support_tickets is missing: the migration must
    stop, not delete the only copy."""
    eng = create_engine(f"sqlite:///{tmp_path / 'nocopy.db'}", future=True)
    with eng.begin() as c:
        for stmt in SEED:
            if "support_tickets" not in stmt:
                c.exec_driver_sql(stmt)
    with eng.begin() as c:
        m = _load(c)
        with pytest.raises(RuntimeError, match="refusing to drop"):
            m.upgrade()
        assert "bug_reports" in _tables(c)
    eng.dispose()


def test_sqlite_downgrade_recreates_empty_tables_and_columns(sqlite_conn):
    m = _load(sqlite_conn)
    m.upgrade()
    m.downgrade()
    assert _tables(sqlite_conn) >= DROPPED
    assert {"subscription_status", "stripe_connect_account_id"} <= _cols(sqlite_conn, "tenants")
    for t in DROPPED:
        assert sqlite_conn.exec_driver_sql(f"SELECT COUNT(*) FROM {t}").scalar() == 0  # noqa: S608 — table names from a test constant
    assert sqlite_conn.exec_driver_sql("SELECT COUNT(*) FROM support_tickets").scalar() == 3, "copied tickets stay"
    m.downgrade()  # rerun must not abort either


def test_sqlite_fresh_install_without_the_tables(tmp_path):
    """A fresh install never had bug_reports (its model is gone): the copy step
    must be skipped, not crash."""
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        c.exec_driver_sql(SEED[0])  # tenants only
    with eng.begin() as c:
        m = _load(c)
        m.upgrade()
        assert {"subscription_status", "stripe_connect_account_id"}.isdisjoint(_cols(c, "tenants"))
    eng.dispose()


# ── Postgres ─────────────────────────────────────

_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 087 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@pytest.fixture()
def pg_conn() -> Generator[Engine, None, None]:
    """Every table lives in a scratch schema — the shared CI database keeps its
    real `users` / `tenants` (an earlier cut dropped them CASCADE)."""
    schema = "m087_scratch"
    admin = create_engine(_URL, future=True)
    with admin.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    admin.dispose()
    eng = create_engine(_URL, future=True, connect_args={"options": f"-c search_path={schema}"})
    with eng.begin() as c:
        for stmt in SEED:
            c.exec_driver_sql(stmt)
        # Prod shape: users.id is uuid while bug_reports.user_id is text — the
        # join must cast, or the copy dies with "operator does not exist:
        # uuid = character varying" (caught on a prod-shaped throwaway 2026-09-06).
        c.exec_driver_sql("ALTER TABLE users ALTER COLUMN id TYPE uuid USING id::uuid")
    with eng.begin() as c:
        yield c
    eng.dispose()
    admin = create_engine(_URL, future=True)
    with admin.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    admin.dispose()


@_requires_pg
def test_pg_upgrade_copies_then_drops(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    _assert_upgraded(pg_conn)


@_requires_pg
def test_pg_upgrade_is_rerunnable(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    m.upgrade()
    _assert_upgraded(pg_conn)


@_requires_pg
def test_pg_downgrade_recreates_and_keeps_the_transaction_healthy(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    m.downgrade()
    m.downgrade()  # already present — IF NOT EXISTS keeps the transaction usable
    assert pg_conn.exec_driver_sql("SELECT 1").scalar() == 1
    assert _tables(pg_conn) >= DROPPED
    assert {"subscription_status", "stripe_connect_account_id"} <= _cols(pg_conn, "tenants")
