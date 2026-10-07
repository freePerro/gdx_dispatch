"""GDXA-352 — ensure_audit_table neither marks a failed install as done nor
commits/rolls back the caller's session.

Before the fix: ``_AUDIT_GUARD_INITIALIZED.add(engine)`` sat outside the
``try``, so one refused DDL (a NOSUPERUSER runtime role) made the absence
permanent for the life of the process; and the DDL ran on the CALLER's
session, so its ``db.commit()`` hardened whatever the caller had staged and its
``db.rollback()`` discarded it.

Each test below fails on the old code for the reason in its docstring. The PG
arm uses the real ``gdx-test-postgres`` fixture and skips without one (fails
under CI, #440).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from gdx_dispatch.core import audit as audit_mod
from gdx_dispatch.core.audit import (
    _AUDIT_GUARD_INITIALIZED,
    _AUDIT_GUARD_RETRY_AT,
    audit_guard_present,
    ensure_audit_table,
)


@pytest.fixture(autouse=True)
def _clean_guard_cache():
    _AUDIT_GUARD_INITIALIZED.clear()
    _AUDIT_GUARD_RETRY_AT.clear()
    yield
    _AUDIT_GUARD_INITIALIZED.clear()
    _AUDIT_GUARD_RETRY_AT.clear()


def _count_statements(engine) -> list[str]:
    seen: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, params, context, executemany):  # noqa: ARG001
        seen.append(" ".join(statement.split())[:40])

    return seen


# ---------------------------------------------------------------- SQLite ---


@pytest.fixture
def sqlite_file_engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'g.db'}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE staged (id INTEGER PRIMARY KEY, v TEXT)"))
    yield eng
    eng.dispose()


def _staged_rows(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM staged")).scalar()


def test_sqlite_install_does_not_harden_the_callers_staged_write(sqlite_file_engine):
    """Old code: the SQLite branch ended in ``db.commit()``, so a row the caller
    had staged was durable before its audit row was even written, and the
    caller's own rollback could no longer undo it."""
    db = sessionmaker(bind=sqlite_file_engine)()
    db.execute(text("INSERT INTO staged (v) VALUES ('pending')"))
    ensure_audit_table(db)
    # The guard is usable inside the caller's transaction...
    assert audit_guard_present(db)
    db.rollback()
    db.close()
    # ...but nothing of the caller's was committed by it.
    assert _staged_rows(sqlite_file_engine) == 0
    # Installed inside the caller's (rolled back) transaction: not marked.
    assert sqlite_file_engine not in _AUDIT_GUARD_INITIALIZED


def test_sqlite_callers_own_flush_error_is_not_reported_as_a_missing_guard(sqlite_file_engine):
    """``begin_nested`` flushes the caller's pending objects. Inside the
    install's ``try`` that swallowed the caller's IntegrityError, logged it as
    ``audit_guard_missing`` and backed the engine off for 300s (/audit,
    2026-10-06). The caller's error is the caller's."""
    from sqlalchemy.exc import IntegrityError

    class _B(DeclarativeBase):
        pass

    class Staged(_B):
        __tablename__ = "staged"
        id: Mapped[int] = mapped_column(primary_key=True)
        v: Mapped[str | None]

    with sqlite_file_engine.begin() as conn:
        conn.execute(text("INSERT INTO staged (id, v) VALUES (1, 'committed')"))
    db = sessionmaker(bind=sqlite_file_engine)()
    db.execute(text("SELECT 1"))  # a transaction is open: the savepoint path
    db.add(Staged(id=1, v="duplicate key"))
    with pytest.raises(IntegrityError):
        ensure_audit_table(db)
    db.rollback()
    db.close()
    assert sqlite_file_engine not in _AUDIT_GUARD_RETRY_AT


def test_sqlite_install_does_not_flush_pending_orm_adds_into_a_commit(sqlite_file_engine):
    """A ``db.add`` with no flush opens no transaction, so "not in a
    transaction" alone would have let the idle-session commit harden it."""
    class _B(DeclarativeBase):
        pass

    class Staged(_B):
        __tablename__ = "staged"
        id: Mapped[int] = mapped_column(primary_key=True)
        v: Mapped[str | None]

    db = sessionmaker(bind=sqlite_file_engine)()
    db.add(Staged(v="pending"))
    ensure_audit_table(db)
    db.rollback()
    db.close()
    assert _staged_rows(sqlite_file_engine) == 0


def test_sqlite_idle_session_installs_commits_and_marks(sqlite_file_engine):
    db = sessionmaker(bind=sqlite_file_engine)()
    ensure_audit_table(db)
    db.close()
    assert sqlite_file_engine in _AUDIT_GUARD_INITIALIZED
    with sqlite_file_engine.connect() as conn:
        assert audit_guard_present(conn)


def test_sqlite_failed_install_is_not_marked_and_backs_off(sqlite_file_engine, caplog):
    """Old code: the engine was marked initialized whatever happened, so a
    second call ran nothing and the guard was never retried."""
    @event.listens_for(sqlite_file_engine, "connect")
    def _read_only(dbapi_conn, record):  # noqa: ARG001
        dbapi_conn.execute("PRAGMA query_only = ON")

    sqlite_file_engine.dispose()  # new connections pick the pragma up
    db = sessionmaker(bind=sqlite_file_engine)()
    with caplog.at_level("ERROR", logger="gdx_dispatch.core.audit"):
        ensure_audit_table(db)
    assert sqlite_file_engine not in _AUDIT_GUARD_INITIALIZED
    assert sqlite_file_engine in _AUDIT_GUARD_RETRY_AT
    assert any("audit_guard_missing" in r.message for r in caplog.records)

    seen = _count_statements(sqlite_file_engine)
    ensure_audit_table(db)  # inside the back-off: no DDL, no read
    assert seen == []

    # Once the back-off lapses and the cause is gone, it installs.
    event.remove(sqlite_file_engine, "connect", _read_only)
    db.close()
    sqlite_file_engine.dispose()
    _AUDIT_GUARD_RETRY_AT[sqlite_file_engine] = 0.0
    db = sessionmaker(bind=sqlite_file_engine)()
    ensure_audit_table(db)
    db.close()
    assert sqlite_file_engine in _AUDIT_GUARD_INITIALIZED


# -------------------------------------------------------------- Postgres ---


@pytest.fixture
def pg_staged(pg_test_engine):
    with pg_test_engine.begin() as conn:
        conn.execute(text("CREATE TABLE gdxa352_staged (id serial PRIMARY KEY, v text)"))
        assert not audit_guard_present(conn), "fixture DB must start guard-absent"
    return pg_test_engine


def _pg_staged_rows(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM gdxa352_staged")).scalar()


@pytest.fixture
def pg_unprivileged_engine(pg_staged, pg_test_db):
    """An engine logged in as a NOSUPERUSER role with DML but no CREATE on
    public and not the owner of audit_logs — the D97 ``gdx_app`` shape."""
    role = f"gdxa352_{uuid.uuid4().hex[:8]}"
    with pg_staged.begin() as conn:
        conn.execute(text(f"CREATE ROLE {role} LOGIN NOSUPERUSER PASSWORD 'x'"))
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        conn.execute(text(f"REVOKE CREATE ON SCHEMA public FROM {role}"))
        conn.execute(text(f"GRANT SELECT, INSERT ON audit_logs, gdxa352_staged TO {role}"))
        conn.execute(text(f"GRANT USAGE ON SEQUENCE gdxa352_staged_id_seq TO {role}"))
    url = make_url(pg_test_db).set(username=role, password="x")
    eng = create_engine(url)
    yield eng
    eng.dispose()
    with pg_staged.begin() as conn:
        conn.execute(text(f"DROP OWNED BY {role}"))
        conn.execute(text(f"DROP ROLE {role}"))


def test_pg_refused_install_keeps_the_callers_staged_write(pg_unprivileged_engine, pg_staged, caplog):
    """Old code: the refused DDL ran on the caller's session and its
    ``db.rollback()`` discarded the row the caller had staged; then the engine
    was marked initialized and never retried."""
    db = sessionmaker(bind=pg_unprivileged_engine)()
    db.execute(text("INSERT INTO gdxa352_staged (v) VALUES ('pending')"))
    with caplog.at_level("ERROR", logger="gdx_dispatch.core.audit"):
        ensure_audit_table(db)
    assert pg_unprivileged_engine not in _AUDIT_GUARD_INITIALIZED
    assert pg_unprivileged_engine in _AUDIT_GUARD_RETRY_AT
    assert any("audit_guard_missing" in r.message for r in caplog.records)
    # The caller's transaction is intact and still its own to commit.
    assert db.execute(text("SELECT count(*) FROM gdxa352_staged")).scalar() == 1
    db.commit()
    db.close()
    assert _pg_staged_rows(pg_staged) == 1


def test_pg_install_does_not_commit_the_callers_staged_write(pg_staged):
    """Old code: ``db.commit()`` after the DDL hardened the caller's staged
    row; the caller's own rollback then could not undo it."""
    db = sessionmaker(bind=pg_staged)()
    db.execute(text("INSERT INTO gdxa352_staged (v) VALUES ('pending')"))
    ensure_audit_table(db)
    assert pg_staged in _AUDIT_GUARD_INITIALIZED
    db.rollback()
    db.close()
    assert _pg_staged_rows(pg_staged) == 0
    with pg_staged.connect() as conn:
        assert audit_guard_present(conn)


def test_pg_install_behind_the_callers_own_lock_times_out_instead_of_hanging(pg_staged):
    """The install now runs on a second connection, and CREATE TRIGGER needs a
    lock on audit_logs that the caller's open transaction can already hold.
    Without ``lock_timeout`` this test would hang forever in one thread."""
    db = sessionmaker(bind=pg_staged)()
    # An earlier audit write in the same transaction: ROW EXCLUSIVE on
    # audit_logs, held, which CREATE TRIGGER's SHARE ROW EXCLUSIVE waits on.
    # (A plain read's ACCESS SHARE does not conflict — measured.)
    db.execute(text(
        "INSERT INTO audit_logs (id, action, entity_type, row_hash, prev_hash, created_at) "
        "VALUES (:id, 't', 't', 'h', '', now())"
    ), {"id": uuid.uuid4()})
    ensure_audit_table(db)
    assert pg_staged not in _AUDIT_GUARD_INITIALIZED
    # A lock timeout is transient: no back-off, the next write retries.
    assert pg_staged not in _AUDIT_GUARD_RETRY_AT
    db.rollback()
    ensure_audit_table(db)
    db.close()
    assert pg_staged in _AUDIT_GUARD_INITIALIZED


def test_pg_function_without_triggers_is_not_treated_as_present(pg_staged):
    """Old code checked pg_proc only: a function with its triggers dropped
    refused nothing and still counted as installed."""
    with pg_staged.begin() as conn:
        audit_mod.install_pg_audit_guard(conn)
        conn.execute(text("DROP TRIGGER audit_logs_no_update ON audit_logs"))
        assert not audit_guard_present(conn)
