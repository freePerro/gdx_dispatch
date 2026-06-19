"""D44 — PG integration tests.

Exercises the PG-specific code paths in ``gdx_dispatch/tools/test_residue_sweep.py``
that SQLite unit tests can't cover:

  - ``~`` regex operator (SQLite uses ``REGEXP``)
  - ``information_schema.columns WHERE table_schema='public'`` (SQLite has
    no information_schema)
  - ``BETWEEN`` on ``timestamp with time zone`` (SQLite has TEXT timestamps
    and a different operator matrix)
  - Transaction rollback semantics when a post-count check fails on PG's
    MVCC model

Runs only under SS-5's PG gate (``run_pg_integration_tests.sh``), which
sets ``GDX_TEST_CONTROL_DB_URL`` to a throwaway postgres:16-alpine URL.
Skipped silently on the default SQLite test run.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from gdx_dispatch.tools.test_residue_sweep import (
    KNOWN_LEGACY_ROWS,
    Finding,
    classify_table,
    delete_residue_in_transaction,
    find_tenant_scoped_tables,
)

PG_URL = os.environ.get("GDX_TEST_CONTROL_DB_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="D44 PG integration tests require GDX_TEST_CONTROL_DB_URL",
)


# Unique table names so parallel/repeat runs don't collide.
_USERS = "_d44_pg_users"
_AUDIT = "_d44_pg_audit"


@pytest.fixture
def pg_engine():
    # No AUTOCOMMIT — the delete-in-transaction path relies on engine.begin()
    # producing a real PG transaction so rollback-on-exception is atomic.
    # The tool's real delete_tenant_residue() creates its engine without
    # AUTOCOMMIT for the same reason; this fixture matches.
    eng = create_engine(PG_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def seed_users(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS "{_USERS}"'))
        conn.execute(text(f"""
            CREATE TABLE "{_USERS}" (
                id TEXT PRIMARY KEY,
                email TEXT,
                company_id TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        conn.execute(text(f"""
            INSERT INTO "{_USERS}" (id, email, company_id) VALUES
                ('u1', 'qa-test@example.com', NULL),
                ('u2', 'someone@example.com', NULL),
                ('u3', 'real@user.com',       NULL),
                ('u4', 'owner@example.com', 'gdx')
        """))
    yield
    with pg_engine.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS "{_USERS}"'))


@pytest.fixture
def seed_audit(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS "{_AUDIT}"'))
        conn.execute(text(f"""
            CREATE TABLE "{_AUDIT}" (
                id TEXT PRIMARY KEY,
                tenant_id TEXT,
                event_type TEXT,
                created_at TIMESTAMPTZ NOT NULL
            )
        """))
        conn.execute(text(f"""
            INSERT INTO "{_AUDIT}" (id, tenant_id, event_type, created_at) VALUES
                ('a1', NULL, 'payment_recorded', '2026-04-08T10:00:00+00:00'),
                ('a2', NULL, 'invoice_sent',     '2026-04-08T23:00:00+00:00'),
                ('a3', NULL, 'recent_event',     '2026-04-15T08:00:00+00:00'),
                ('a4', 'gdx', 'scoped_event',    '2026-04-08T12:00:00+00:00')
        """))
    yield
    with pg_engine.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS "{_AUDIT}"'))


# ── PG regex operator (~) ──────────────────────────────────────────────────

def test_pg_regex_operator_classifies_residue_vs_unattributed(
    pg_engine, seed_users, monkeypatch,
):
    """The ~ operator is what production PG uses. Confirm RESIDUE_PATTERNS
    classifies rows as residue only when the pattern matches on live PG."""
    # Patch RESIDUE_PATTERNS to register the throwaway table for this test.
    from gdx_dispatch.tools import test_residue_sweep as mod
    monkeypatch.setitem(
        mod.RESIDUE_PATTERNS,
        _USERS,
        [("email", r"^qa[-_]test@|@example\.(com|org)$")],
    )

    result = classify_table(pg_engine, text, _USERS, {"company_id"},
                            tenant_slug="testtenant")
    assert result is not None
    # u1 (qa-test@) + u2 (someone@example.com) match → residue
    # u3 (real@user.com) doesn't match → unattributed
    # u4 has scope → not counted
    assert result.deletable_residue_count == 2, \
        f"expected 2 residue, got {result}"
    assert result.unattributed_count == 1
    assert result.null_column == "company_id"


# ── information_schema walker ──────────────────────────────────────────────

def test_find_tenant_scoped_tables_sees_throwaway_table(pg_engine, seed_users):
    """The info_schema walker must see our test table under schema='public'."""
    tables = find_tenant_scoped_tables(pg_engine, text)
    assert _USERS in tables
    assert tables[_USERS] == {"company_id"}


# ── BETWEEN on timestamptz ──────────────────────────────────────────────────

def test_acknowledged_legacy_narrows_by_timestamptz_range(
    pg_engine, seed_audit, monkeypatch,
):
    """PG timestamptz comparison with explicit UTC offsets: the in-range
    rows count as acknowledged; out-of-range rows count as unattributed."""
    monkeypatch.setitem(
        KNOWN_LEGACY_ROWS,
        ("testtenant", _AUDIT, "tenant_id"),
        {
            "date_range_utc": ("2026-04-08T00:00:00+00:00",
                               "2026-04-08T23:59:59.999+00:00"),
            "reason": "test",
            "acknowledged_at": "2026-04-17",
            "filed_by": "test",
        },
    )
    result = classify_table(pg_engine, text, _AUDIT, {"tenant_id"},
                            tenant_slug="testtenant")
    assert result is not None
    # a1, a2 inside range → acknowledged_legacy (2)
    # a3 outside range → unattributed (1)
    # a4 has scope → not counted
    assert result.acknowledged_legacy_count == 2
    assert result.unattributed_count == 1
    assert result.deletable_residue_count == 0


# ── DELETE path against real PG ────────────────────────────────────────────

def test_delete_residue_survives_real_pg_transaction(
    pg_engine, seed_users, monkeypatch,
):
    """The transaction wrapper + post-count verify must work on PG MVCC."""
    from gdx_dispatch.tools import test_residue_sweep as mod
    monkeypatch.setitem(
        mod.RESIDUE_PATTERNS,
        _USERS,
        [("email", r"^qa[-_]test@|@example\.(com|org)$")],
    )

    findings = [Finding(
        table=_USERS,
        deletable_residue_count=2,
        unattributed_count=1,
        null_column="company_id",
    )]
    deleted = delete_residue_in_transaction(pg_engine, text, findings)
    assert deleted == 2

    with pg_engine.connect() as conn:
        rows = conn.execute(
            text(f'SELECT id, email FROM "{_USERS}" ORDER BY id')
        ).fetchall()
    # Only u3 (unattributed) and u4 (scoped) survive — residue rows gone.
    assert sorted(r[0] for r in rows) == ["u3", "u4"]


def test_delete_rolls_back_on_count_mismatch_pg(
    pg_engine, seed_users, monkeypatch,
):
    """Lie about the expected count → PG transaction rolls back atomically."""
    from gdx_dispatch.tools import test_residue_sweep as mod
    monkeypatch.setitem(
        mod.RESIDUE_PATTERNS,
        _USERS,
        [("email", r"^qa[-_]test@")],  # matches only u1
    )

    findings = [Finding(
        table=_USERS,
        deletable_residue_count=5,  # lie: actually 1
        unattributed_count=0,
        null_column="company_id",
    )]
    with pytest.raises(RuntimeError, match="expected 5"):
        delete_residue_in_transaction(pg_engine, text, findings)

    # PG should have rolled back — all 4 seed rows still present
    with pg_engine.connect() as conn:
        count = conn.execute(
            text(f'SELECT COUNT(*) FROM "{_USERS}"')
        ).scalar()
    assert count == 4
