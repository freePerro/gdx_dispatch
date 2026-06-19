"""D45 unit tests — audit_logs immutability triggers on SQLite.

The PG path (real trigger semantics + plpgsql) is covered by
gdx_dispatch/tests/integration/test_audit_immutability_pg.py under the SS-5 gate.
These tests exercise the SQLite branch of ensure_audit_table, which
gained a BEFORE UPDATE trigger in D45 (previously only DELETE was blocked).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import _AUDIT_GUARD_INITIALIZED, ensure_audit_table


@pytest.fixture
def sqlite_session():
    # Each test gets a fresh in-memory engine. Clear the guard cache so
    # ensure_audit_table actually runs the DDL on this engine.
    _AUDIT_GUARD_INITIALIZED.clear()
    eng = create_engine("sqlite:///:memory:")
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    eng.dispose()
    _AUDIT_GUARD_INITIALIZED.clear()


def _seed_row(session, row_id: str = "r1") -> None:
    session.execute(
        text(
            "INSERT INTO audit_logs "
            "(id, action, entity_type, row_hash, prev_hash, created_at) "
            "VALUES (:id, 'test.action', 'test', 'hash-abc', '', CURRENT_TIMESTAMP)"
        ),
        {"id": row_id},
    )
    session.commit()


def test_ensure_audit_table_installs_both_triggers_on_sqlite(sqlite_session):
    ensure_audit_table(sqlite_session)
    rows = sqlite_session.execute(
        text("SELECT name FROM sqlite_master WHERE type='trigger'")
    ).fetchall()
    names = {r[0] for r in rows}
    assert "audit_logs_no_delete" in names
    assert "audit_logs_no_update" in names, \
        "D45: UPDATE trigger must also be installed"


def test_delete_raises_after_ensure_audit_table(sqlite_session):
    ensure_audit_table(sqlite_session)
    _seed_row(sqlite_session)
    with pytest.raises(Exception, match="audit_logs is immutable"):
        sqlite_session.execute(text("DELETE FROM audit_logs WHERE id = :id"),
                               {"id": "r1"})
        sqlite_session.commit()


def test_update_raises_after_ensure_audit_table(sqlite_session):
    """D45: the gap we just closed. UPDATE must raise, not silently succeed."""
    ensure_audit_table(sqlite_session)
    _seed_row(sqlite_session)
    with pytest.raises(Exception, match="audit_logs is immutable"):
        sqlite_session.execute(
            text("UPDATE audit_logs SET tenant_id = 'leak' WHERE id = :id"),
            {"id": "r1"},
        )
        sqlite_session.commit()


def test_insert_still_works_after_ensure_audit_table(sqlite_session):
    """Immutability must not block INSERT — append-only is the point."""
    ensure_audit_table(sqlite_session)
    _seed_row(sqlite_session, "r1")
    _seed_row(sqlite_session, "r2")
    count = sqlite_session.execute(
        text("SELECT COUNT(*) FROM audit_logs")
    ).scalar()
    assert count == 2


def test_ensure_is_idempotent(sqlite_session):
    """Second call must not error (triggers use IF NOT EXISTS on SQLite)."""
    ensure_audit_table(sqlite_session)
    _AUDIT_GUARD_INITIALIZED.clear()  # force re-run
    ensure_audit_table(sqlite_session)  # would raise on duplicate-trigger
    _seed_row(sqlite_session)
    count = sqlite_session.execute(
        text("SELECT COUNT(*) FROM audit_logs")
    ).scalar()
    assert count == 1
