"""D45 PG integration tests — audit_logs immutability triggers on real PG.

Exercises the PG branch of ensure_audit_table() that SQLite unit tests
can't cover: plpgsql function + BEFORE UPDATE/DELETE triggers.

Runs only under SS-5's PG gate (GDX_TEST_CONTROL_DB_URL set). The test
creates a standalone audit_logs-shaped table (schema matches the ORM
model), installs the triggers via ensure_audit_table, and verifies
INSERT works + UPDATE/DELETE raise + reinstall is idempotent.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import _AUDIT_GUARD_INITIALIZED, ensure_audit_table

PG_URL = os.environ.get("GDX_TEST_CONTROL_DB_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="D45 PG tests require GDX_TEST_CONTROL_DB_URL",
)


@pytest.fixture
def pg_session():
    """Session against the throwaway PG. Creates + drops the audit_logs table
    so we don't contaminate the platform schema the PG gate already applied."""
    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS audit_logs CASCADE"))
        conn.execute(text("""
            CREATE TABLE audit_logs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT,
                user_id TEXT,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT,
                details JSONB,
                ip_address TEXT,
                request_id TEXT,
                row_hash TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                event_type TEXT,
                actor_id TEXT,
                actor_role TEXT,
                payload JSONB,
                hash TEXT
            )
        """))
    _AUDIT_GUARD_INITIALIZED.clear()
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS audit_logs CASCADE"))
        conn.execute(text("DROP FUNCTION IF EXISTS audit_logs_immutable_guard() CASCADE"))
    eng.dispose()
    _AUDIT_GUARD_INITIALIZED.clear()


def _seed(session, row_id: str = "r1") -> None:
    session.execute(text("""
        INSERT INTO audit_logs
          (id, action, entity_type, row_hash, prev_hash)
          VALUES (:id, 'test.action', 'test', 'hash-abc', '')
    """), {"id": row_id})
    session.commit()


def test_pg_ensure_audit_table_installs_both_triggers(pg_session):
    ensure_audit_table(pg_session)
    rows = pg_session.execute(text(
        "SELECT tgname FROM pg_trigger "
        "WHERE tgrelid = 'audit_logs'::regclass AND NOT tgisinternal"
    )).fetchall()
    names = {r.tgname for r in rows}
    assert "audit_logs_no_update" in names
    assert "audit_logs_no_delete" in names


def test_pg_insert_still_works(pg_session):
    ensure_audit_table(pg_session)
    _seed(pg_session, "r1")
    _seed(pg_session, "r2")
    count = pg_session.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar()
    assert count == 2


def test_pg_delete_raises_after_install(pg_session):
    ensure_audit_table(pg_session)
    _seed(pg_session)
    with pytest.raises(Exception, match="audit_logs is immutable"):
        pg_session.execute(text("DELETE FROM audit_logs WHERE id = :id"),
                           {"id": "r1"})
        pg_session.commit()
    # Row must survive the failed delete
    pg_session.rollback()
    count = pg_session.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar()
    assert count == 1


def test_pg_update_raises_after_install(pg_session):
    """D45 core guarantee: a raw UPDATE that would corrupt the row_hash
    chain is blocked at the DB layer, not just by code discipline."""
    ensure_audit_table(pg_session)
    _seed(pg_session)
    with pytest.raises(Exception, match="audit_logs is immutable"):
        pg_session.execute(
            text("UPDATE audit_logs SET tenant_id = 'leak' WHERE id = :id"),
            {"id": "r1"},
        )
        pg_session.commit()
    # Row's tenant_id must still be NULL (unchanged from seed)
    pg_session.rollback()
    row = pg_session.execute(
        text("SELECT tenant_id FROM audit_logs WHERE id = 'r1'")
    ).fetchone()
    assert row[0] is None


def test_pg_ensure_is_idempotent(pg_session):
    """CREATE OR REPLACE FUNCTION + DROP TRIGGER IF EXISTS means a second
    call must be a no-op, not an error."""
    ensure_audit_table(pg_session)
    _AUDIT_GUARD_INITIALIZED.clear()
    ensure_audit_table(pg_session)
    _seed(pg_session)
    count = pg_session.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar()
    assert count == 1
    # Still only 2 triggers, not 4
    rows = pg_session.execute(text(
        "SELECT tgname FROM pg_trigger "
        "WHERE tgrelid = 'audit_logs'::regclass AND NOT tgisinternal"
    )).fetchall()
    names = [r.tgname for r in rows]
    assert names.count("audit_logs_no_update") == 1
    assert names.count("audit_logs_no_delete") == 1
