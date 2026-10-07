"""`ensure_audit_table`'s Postgres fallback leaves the caller's transaction alone (GDXA-350).

No migration installs `audit_logs_immutable_guard`, so on a database that was not
paved the first audit write per engine finds it absent and tries the DDL. As the
runtime role (no CREATE on schema public) that DDL is refused, and the fallback
used to answer with `db.rollback()` on the CALLER's session. The audit row
written next survived and the work staged before it did not: a payment reversal
on a cold worker kept "payment_reversed" in the trail and lost the void. When
the DDL succeeded (owning role) it answered with `db.commit()`, hardening the
caller's work before its audit row existed.

Each test stages a row, writes an audit row through `log_audit_event_sync`
(which calls `ensure_audit_table` on the way in), and checks what the caller's
own commit or rollback decides. On the old code the first test loses the staged
row and the second finds it committed despite the rollback.

Postgres only, because the defect is the PG branch. The arm skips without a
reachable test server and fails under CI, like every PG arm here.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core import audit as audit_mod
from gdx_dispatch.core.audit import (
    _AUDIT_GUARD_INITIALIZED,
    _AUDIT_GUARD_RETRY_AT,
    log_audit_event_sync,
)

_GUARD_EXISTS = "SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'audit_logs_immutable_guard')"


def _setup_probe_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE gdxa350_staged (id integer PRIMARY KEY)"))
        assert conn.execute(text(_GUARD_EXISTS)).scalar() is False  # precondition


def _staged_ids(engine) -> list[int]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text("SELECT id FROM gdxa350_staged ORDER BY id"))]


def _audit_actions(engine) -> list[str]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text("SELECT action FROM audit_logs"))]


def _guard_exists(engine) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(text(_GUARD_EXISTS)).scalar())


def _stage_and_audit(session, row_id: int, action: str) -> None:
    session.execute(text("INSERT INTO gdxa350_staged (id) VALUES (:i)"), {"i": row_id})
    log_audit_event_sync(session, action=action, entity_type="gdxa350", entity_id=str(row_id))


@pytest.fixture
def runtime_role_engine(pg_test_db):
    """An engine whose every connection runs as a role that cannot CREATE in
    schema public — the shape of production's ``gdx_app``."""
    owner = create_engine(pg_test_db, future=True)
    role = f"gdxa350_app_{uuid4().hex[:8]}"
    _setup_probe_table(owner)
    with owner.begin() as conn:
        conn.execute(text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER"))
        conn.execute(text(f"REVOKE CREATE ON SCHEMA public FROM PUBLIC, {role}"))
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        conn.execute(text(f"GRANT SELECT, INSERT ON audit_logs, gdxa350_staged TO {role}"))
    app_engine = create_engine(pg_test_db, future=True)

    @event.listens_for(app_engine, "connect")
    def _as_runtime_role(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute(f"SET ROLE {role}")
        cur.close()

    try:
        yield owner, app_engine
    finally:
        app_engine.dispose()
        with owner.begin() as conn:
            conn.execute(text(f"DROP OWNED BY {role}"))
            conn.execute(text(f"DROP ROLE {role}"))
        owner.dispose()


def test_refused_guard_ddl_keeps_the_callers_staged_work(runtime_role_engine, caplog):
    owner, app_engine = runtime_role_engine
    SessionLocal = sessionmaker(bind=app_engine, autoflush=False, autocommit=False)

    with caplog.at_level(logging.ERROR, logger="gdx_dispatch.core.audit"), SessionLocal() as db:
        _stage_and_audit(db, 1, "payment_reversed")
        db.commit()

    assert _staged_ids(owner) == [1], "the refused DDL discarded the caller's staged work"
    assert _audit_actions(owner) == ["payment_reversed"]
    assert not _guard_exists(owner)
    # A privilege refusal is not retried on every write, and it says so loudly.
    # Nor is it cached for the process: it backs off and retries (GDXA-352).
    assert app_engine not in _AUDIT_GUARD_INITIALIZED
    assert app_engine in _AUDIT_GUARD_RETRY_AT
    assert any("audit_guard_missing" in r.getMessage() for r in caplog.records)

    # The next write on the warm engine is unaffected.
    with SessionLocal() as db:
        _stage_and_audit(db, 2, "invoice_reopened")
        db.commit()
    assert _staged_ids(owner) == [1, 2]


def test_installed_guard_does_not_commit_the_callers_work(pg_test_engine):
    engine = pg_test_engine
    _setup_probe_table(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # The owning role CAN install the guard. The caller then rolls back:
    # nothing it staged may have been committed on its behalf. The guard is
    # committed on its own connection, so it stays and the engine is warm.
    with SessionLocal() as db:
        _stage_and_audit(db, 1, "staged_then_abandoned")
        db.rollback()
    assert _staged_ids(engine) == [], "the guard DDL committed the caller's staged work"
    assert _audit_actions(engine) == []
    assert _guard_exists(engine)
    assert engine in _AUDIT_GUARD_INITIALIZED

    with SessionLocal() as db:
        _stage_and_audit(db, 2, "staged_then_committed")
        db.commit()
    assert _staged_ids(engine) == [2]
    assert _audit_actions(engine) == ["staged_then_committed"]


def test_installing_the_guard_does_not_lock_audit_logs_for_the_callers_transaction(pg_test_engine):
    """A successful install inside the caller's transaction would hold
    ShareRowExclusiveLock on audit_logs until that caller commits, and every
    other connection's audit INSERT would wait behind it."""
    engine = pg_test_engine
    _setup_probe_table(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with SessionLocal() as db:
        _stage_and_audit(db, 1, "first_write_uncommitted")
        assert _guard_exists(engine)
        with engine.connect() as other:
            other.execute(text("SET lock_timeout = '1500ms'"))
            other.execute(
                text(
                    "INSERT INTO audit_logs (id, action, entity_type, row_hash, prev_hash, created_at) "
                    "VALUES (:id, 'other_writer', 'gdxa350', 'x', '', now())"
                ),
                {"id": uuid4()},
            )
            other.commit()
        db.commit()
    assert sorted(_audit_actions(engine)) == ["first_write_uncommitted", "other_writer"]


def test_a_failure_other_than_privilege_is_retried(pg_test_engine, monkeypatch):
    engine = pg_test_engine
    _setup_probe_table(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _flaky(db):
        db.execute(text("SELECT 1"))
        raise RuntimeError("could not obtain lock on relation audit_logs")

    monkeypatch.setattr(audit_mod, "install_pg_audit_guard", _flaky)
    with SessionLocal() as db:
        _stage_and_audit(db, 1, "during_flake")
        db.commit()
    assert _staged_ids(engine) == [1]
    assert engine not in _AUDIT_GUARD_INITIALIZED, "a transient failure was cached for the process"

    monkeypatch.undo()
    with SessionLocal() as db:
        _stage_and_audit(db, 2, "after_flake")
        db.commit()
    assert _guard_exists(engine)
