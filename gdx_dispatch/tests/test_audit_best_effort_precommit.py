"""GDXA-63: a refused audit row must not cost a caller the work it has STAGED.

``audit_best_effort`` is for a caller that has already committed; it commits.
``core/payments.py:_audit_payment_reversal`` audits a Stripe reversal *before*
its caller's commit, with the void and the invoice transition still staged, and
swallows the failure in a bare ``try/except``. A refused flush deactivates the
session, so the caller's ``db.commit()`` raises ``PendingRollbackError``: the
webhook 500s, the reversal is lost, and Stripe redelivers it.
``audit_best_effort_precommit`` is the helper for that shape.

The refusal comes from the storage layer — a real ``BEFORE INSERT`` trigger on
``audit_logs`` — never a Python patch, which would raise before ``flush()`` and
never deactivate anything. Every failure-path test asserts the caller's own
mutation persisted, not merely that the audit row is absent: on a path that
rolls the caller back, "no audit row, no exception" reads healthy while the
money is gone.

"cold" is a fresh engine, so ``ensure_audit_table`` installs the guard inside
the call, in front of staged rows; "warm" installs it first on an idle session.
"""
from __future__ import annotations

import contextlib
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError, PendingRollbackError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import (
    AuditLog,
    TenantBase,
    audit_best_effort_precommit,
    ensure_audit_table,
    log_audit_event_sync,
)
from gdx_dispatch.models.tenant_models import User

TENANT = "11111111-1111-1111-1111-111111111463"


def _engine(*, refusing: bool, warm: bool):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    if refusing:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TRIGGER audit_logs_refuse_insert
                    BEFORE INSERT ON audit_logs
                    BEGIN
                        SELECT RAISE(ABORT, 'audit storage refuses this row');
                    END;
                    """
                )
            )
    # Production's sessionmaker (core/database.py) leaves expire_on_commit at
    # its default True; so does this one.
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    if warm:
        with SessionLocal() as idle:
            ensure_audit_table(idle)
    return engine, SessionLocal


def _user(uid, name: str) -> User:
    return User(
        id=uid, username=name, email=f"{uid.hex[:8]}@example.com",
        role="technician", company_id=TENANT, active=True,
    )


def _seed(SessionLocal):
    """A committed row the caller will modify, as a reversal modifies an invoice."""
    uid = uuid4()
    with SessionLocal() as db:
        db.add(_user(uid, "before"))
        db.commit()
    return uid


def _stage(db, existing):
    """Stage both kinds of work a reversal stages: an UPDATE and an INSERT.

    The UPDATE is re-dirtied AFTER the flush, as ``_reverse_recorded_payment``
    re-opens the invoice after flushing the void — so the helper is handed a
    session with flushed-but-uncommitted rows AND unflushed changes.
    """
    held = db.execute(select(User).where(User.id == existing)).scalars().one()
    held.username = "flushed"
    new_id = uuid4()
    db.add(_user(new_id, "inserted"))
    db.flush()
    held.username = "after-flush"
    return held, new_id


def _durable(SessionLocal, existing, new_id) -> tuple[str, bool]:
    with SessionLocal() as db:
        name = db.execute(select(User.username).where(User.id == existing)).scalar_one()
        inserted = db.execute(select(User).where(User.id == new_id)).scalars().first()
        return name, inserted is not None


def _audit_rows(SessionLocal, action: str) -> int:
    with SessionLocal() as db:
        return len(db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all())


def _call(db, action: str) -> bool:
    return audit_best_effort_precommit(
        db, action=action, entity_type="user", entity_id="u-1",
        tenant_id=TENANT, user_id="stripe-webhook", details={"k": "v"},
    )


STATES = pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])


# ── positive control: the harness can see the defect ─────────────────────────


@STATES
def test_control_the_swallowed_flush_loses_the_staged_work(warm):
    """Today's ``_audit_payment_reversal`` shape, in this fixture: red.

    If this ever stops raising, the fixture no longer produces a deactivated
    session and every green test below is measuring nothing.
    """
    engine, SessionLocal = _engine(refusing=True, warm=warm)
    try:
        existing = _seed(SessionLocal)
        with SessionLocal() as db:
            _stage(db, existing)
            ensure_audit_table(db)
            with contextlib.suppress(Exception):
                log_audit_event_sync(
                    db, tenant_id=TENANT, user_id="stripe-webhook", action="reversed",
                    entity_type="user", entity_id="u-1", details={},
                )
            with pytest.raises(PendingRollbackError):
                db.commit()
        assert _durable(SessionLocal, existing, uuid4())[0] == "before", "the staged work was lost"
    finally:
        engine.dispose()


def test_control_the_fixture_expires_on_commit_like_production():
    engine, SessionLocal = _engine(refusing=False, warm=True)
    try:
        existing = _seed(SessionLocal)
        with SessionLocal() as db:
            held, _ = _stage(db, existing)
            db.commit()
            assert sa_inspect(held).expired is True
    finally:
        engine.dispose()


# ── the helper ────────────────────────────────────────────────────────────────


@STATES
def test_refused_trail_the_staged_work_still_commits(warm, caplog):
    """The defect, fixed. Fails if the savepoint is removed (the caller's
    commit raises ``PendingRollbackError``) or if a rollback is added in the
    ``except`` (the staged work is discarded)."""
    engine, SessionLocal = _engine(refusing=True, warm=warm)
    try:
        existing = _seed(SessionLocal)
        with SessionLocal() as db:
            _, new_id = _stage(db, existing)
            with caplog.at_level("ERROR"):
                assert _call(db, "reversed") is False
            db.commit()  # the caller's own commit — must not raise

        assert _durable(SessionLocal, existing, new_id) == ("after-flush", True), (
            "the caller's staged UPDATE and INSERT must both be durable"
        )
        assert _audit_rows(SessionLocal, "reversed") == 0
        assert "audit_best_effort_precommit_failed" in caplog.text, "a lost row is never silent"
    finally:
        engine.dispose()


@STATES
def test_healthy_trail_commits_with_the_caller(warm):
    engine, SessionLocal = _engine(refusing=False, warm=warm)
    try:
        existing = _seed(SessionLocal)
        with SessionLocal() as db:
            _, new_id = _stage(db, existing)
            assert _call(db, "reversed") is True
            db.commit()

        assert _durable(SessionLocal, existing, new_id) == ("after-flush", True)
        assert _audit_rows(SessionLocal, "reversed") == 1
    finally:
        engine.dispose()


@STATES
def test_the_helper_never_commits(warm):
    """No commit, cold or warm: a caller that then rolls back keeps neither
    its work nor the trail row. Fails if anything in the call commits —
    including ``ensure_audit_table``'s first-use install on a cold engine."""
    engine, SessionLocal = _engine(refusing=False, warm=warm)
    commits = []
    try:
        existing = _seed(SessionLocal)
        # The engine's ``commit`` event, not the session's ``after_commit``:
        # the latter also fires when a savepoint is RELEASEd.
        event.listen(engine, "commit", lambda conn: commits.append(conn))
        with SessionLocal() as db:
            _, new_id = _stage(db, existing)
            assert _call(db, "reversed") is True
            assert commits == []
            db.rollback()

        assert _durable(SessionLocal, existing, new_id) == ("before", False)
        assert _audit_rows(SessionLocal, "reversed") == 0
    finally:
        engine.dispose()


def test_sqlite_nothing_staged_the_row_commits_on_its_own():
    """The precondition's SQLite edge, pinned so the docstring stays honest.

    With nothing flushed, the pysqlite driver has sent no ``BEGIN`` (it begins
    only before DML), so ``begin_nested()`` opens the transaction with its own
    ``SAVEPOINT`` and the ``RELEASE`` commits the row outright — no engine
    ``commit`` event fires, and the caller's rollback cannot take it back.
    Postgres has no such edge (``test_postgres_...``). If this starts failing,
    the driver changed, and the docstring's SQLite caveat should go with it.
    """
    engine, SessionLocal = _engine(refusing=False, warm=True)
    commits = []
    try:
        existing = _seed(SessionLocal)
        event.listen(engine, "commit", lambda conn: commits.append(conn))
        with SessionLocal() as db:
            db.execute(select(User).where(User.id == existing)).scalars().one()
            assert _call(db, "read_only") is True
            db.rollback()
        assert commits == []
        assert _audit_rows(SessionLocal, "read_only") == 1
    finally:
        engine.dispose()


def test_a_session_that_arrives_dead_is_not_rolled_back():
    """No rollback, not even a conditional one.

    A caller that broke its own session before auditing has staged work that
    is already lost. Rolling back here would make its next ``commit()``
    succeed with nothing in it — a 200 for a reversal that never happened.
    Left alone, that commit raises. Fails if ``audit_best_effort``'s
    ``if not db.is_active: db.rollback()`` is copied in.
    """
    engine, SessionLocal = _engine(refusing=True, warm=True)
    try:
        existing = _seed(SessionLocal)
        with SessionLocal() as db:
            _stage(db, existing)
            with contextlib.suppress(Exception):
                log_audit_event_sync(
                    db, tenant_id=TENANT, user_id="u", action="earlier",
                    entity_type="thing", entity_id="0", details={},
                )
            assert db.is_active is False, "precondition: the caller broke its own session"

            assert _call(db, "reversed") is False
            with pytest.raises(PendingRollbackError):
                db.commit()
    finally:
        engine.dispose()


def test_an_error_in_the_callers_own_data_raises_as_the_callers(caplog):
    """The helper flushes the caller's work outside its ``try``: a constraint
    violation in that work is the caller's error, not a lost audit row."""
    engine, SessionLocal = _engine(refusing=False, warm=True)
    try:
        existing = _seed(SessionLocal)
        with SessionLocal() as db, caplog.at_level("ERROR"):
            db.add(_user(existing, "duplicate primary key"))
            with pytest.raises(IntegrityError):
                _call(db, "reversed")
        assert "audit_best_effort_precommit_failed" not in caplog.text
    finally:
        engine.dispose()


# ── Postgres ──────────────────────────────────────────────────────────────────
#
# Skips without a reachable server, like every PG arm here; fails under CI.


def test_postgres_staged_work_survives_a_refused_trail(pg_test_engine):
    """Healthy, never-commits, then refused — against PG with a real trigger.

    The caller's work is a scratch table: the template is loaded from
    ``structure.sql``, which has drifted from the ORM's ``users``.
    """
    SessionLocal = sessionmaker(bind=pg_test_engine, autoflush=False, autocommit=False)
    with pg_test_engine.begin() as conn:
        conn.execute(text("CREATE TABLE caller_work (id text PRIMARY KEY)"))

    def _caller_rows() -> set[str]:
        with pg_test_engine.connect() as conn:
            return set(conn.execute(text("SELECT id FROM caller_work")).scalars())

    # healthy: the row lands with the caller's commit
    with SessionLocal() as db:
        db.execute(text("INSERT INTO caller_work (id) VALUES ('w1')"))
        assert _call(db, "pg_healthy") is True
        db.commit()
    assert _caller_rows() == {"w1"}
    assert _audit_rows(SessionLocal, "pg_healthy") == 1

    # never commits: the caller rolls back and both are gone
    with SessionLocal() as db:
        db.execute(text("INSERT INTO caller_work (id) VALUES ('w2')"))
        assert _call(db, "pg_rolled_back") is True
        db.rollback()
    assert _caller_rows() == {"w1"}
    assert _audit_rows(SessionLocal, "pg_rolled_back") == 0

    # nothing staged: unlike SQLite, the SAVEPOINT nests inside the
    # transaction the driver opened for the read, so the rollback takes it
    with SessionLocal() as db:
        db.execute(text("SELECT id FROM caller_work")).all()
        assert _call(db, "pg_read_only") is True
        db.rollback()
    assert _audit_rows(SessionLocal, "pg_read_only") == 0

    with pg_test_engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE FUNCTION audit_logs_refuse() RETURNS trigger AS $$
                BEGIN RAISE EXCEPTION 'audit storage refuses this row'; END;
                $$ LANGUAGE plpgsql
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER audit_logs_refuse_insert
                    BEFORE INSERT ON audit_logs
                    FOR EACH ROW EXECUTE FUNCTION audit_logs_refuse()
                """
            )
        )

    # refused: the savepoint contains the 25P02, the caller's commit succeeds
    with SessionLocal() as db:
        db.execute(text("INSERT INTO caller_work (id) VALUES ('w3')"))
        assert _call(db, "pg_refused") is False
        db.commit()
    assert _caller_rows() == {"w1", "w3"}, "the staged row committed despite the refusal"
    assert _audit_rows(SessionLocal, "pg_refused") == 0
