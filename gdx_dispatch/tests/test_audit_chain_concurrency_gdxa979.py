"""Concurrent audited writes must not fork the hash chain — GDXA-979.

``_log_audit_event_impl`` read the chain tip with a plain SELECT and inserted
after it, with nothing between the read and the commit. Two transactions open
at once read the same tip and both wrote it as their ``prev_hash``: a fork.
These tests hold each writer's transaction open after its audit row, the way a
request handler does between ``log_audit_event`` and ``commit``, so a second
writer reads the tip inside that window.

Postgres is the production database and the one the race was measured on;
SQLite is the dev/test database and serializes differently, so both run.

The verifier half: a fork must raise AUDIT_CHAIN_BROKEN even when unchained
(GL-written) rows exist, which used to downgrade every failure to a WARN.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import Counter
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import (
    AuditLog,
    TenantBase,
    audit_chain_report,
    log_audit_event_sync,
    verify_audit_chain,
)

WRITERS = 4
ROUNDS = 5


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, tables=[AuditLog.__table__])
    with sessionmaker(bind=engine, autoflush=False)() as db:
        yield db
    engine.dispose()


def _race(engine) -> list[BaseException]:
    """WRITERS threads, ROUNDS times: all write an audit row at the same moment,
    then hold the transaction open before committing."""
    factory = sessionmaker(bind=engine)
    errors: list[BaseException] = []
    for rnd in range(ROUNDS):
        barrier = threading.Barrier(WRITERS)

        def writer(i: int, rnd: int = rnd, barrier: threading.Barrier = barrier) -> None:
            try:
                with factory() as s:
                    barrier.wait(timeout=10)
                    log_audit_event_sync(
                        s, tenant_id="t", user_id=f"u{i}", action="race",
                        entity_type="job", entity_id=f"{rnd}-{i}", details={"i": i},
                    )
                    time.sleep(0.1)  # the handler's work between audit and commit
                    s.commit()
            except BaseException as exc:  # noqa: BLE001 — reported by the assert below
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(WRITERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
    return errors


def _assert_one_linear_chain(engine) -> None:
    with sessionmaker(bind=engine)() as s:
        rows = s.execute(select(AuditLog.prev_hash, AuditLog.row_hash)).all()
        assert len(rows) == WRITERS * ROUNDS
        dupes = {p: n for p, n in Counter(r.prev_hash for r in rows).items() if n > 1}
        assert dupes == {}, f"{len(dupes)} prev_hash values are shared: the chain forked"
        # Exactly one row starts the chain, and the created_at order walks it.
        assert sum(1 for r in rows if r.prev_hash == "") == 1
        report = audit_chain_report(s)
        assert report["forks"] == [] and report["restarts"] == 0 and report["broken"] is False
        assert verify_audit_chain(s) is True


@pytest.fixture
def pg_engine(pg_test_engine):
    TenantBase.metadata.create_all(pg_test_engine, tables=[AuditLog.__table__])
    with pg_test_engine.begin() as conn:
        conn.execute(text("TRUNCATE audit_logs"))
    return pg_test_engine


def test_postgres_concurrent_audit_writes_build_one_chain(pg_engine):
    # Warm the guard install so the race is on the tip, not on the DDL.
    with sessionmaker(bind=pg_engine)() as s:
        from gdx_dispatch.core.audit import ensure_audit_table

        ensure_audit_table(s)
        s.commit()
    errors = _race(pg_engine)
    assert errors == []
    _assert_one_linear_chain(pg_engine)


def test_postgres_nested_session_audit_fails_instead_of_hanging(pg_engine, monkeypatch):
    """A thread auditing on a second session while its first holds the chain
    lock waits on itself; the bounded wait turns that into an error. The
    caller's own lock_timeout is put back after the lock is taken."""
    from sqlalchemy.exc import OperationalError

    import gdx_dispatch.core.audit as audit

    monkeypatch.setattr(audit, "_CHAIN_LOCK_TIMEOUT", "300ms")
    factory = sessionmaker(bind=pg_engine)
    with factory() as first, factory() as second:
        log_audit_event_sync(first, tenant_id="t", user_id="u", action="a", entity_type="job", entity_id="1")
        assert first.execute(text("SHOW lock_timeout")).scalar() == "0"
        started = time.monotonic()
        with pytest.raises(OperationalError, match="lock timeout"):
            log_audit_event_sync(second, tenant_id="t", user_id="u", action="b", entity_type="job", entity_id="2")
        assert time.monotonic() - started < 5
        second.rollback()
        first.commit()


def test_sqlite_concurrent_audit_writes_build_one_chain(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'audit.db'}", connect_args={"check_same_thread": False, "timeout": 30}
    )
    TenantBase.metadata.create_all(engine, tables=[AuditLog.__table__])
    try:
        errors = _race(engine)
        assert errors == []
        _assert_one_linear_chain(engine)
    finally:
        engine.dispose()


def test_tip_skips_an_unchained_row(db_session):
    """A GL-written row (empty row_hash) is never the predecessor: chaining to
    it would restart the chain at prev_hash=''."""
    log_audit_event_sync(db_session, tenant_id="t", user_id="u", action="a", entity_type="job", entity_id="1")
    db_session.commit()
    first = db_session.execute(select(AuditLog.row_hash)).scalar_one()
    db_session.execute(
        text(
            "INSERT INTO audit_logs (id, tenant_id, user_id, action, entity_type, entity_id, details, "
            "prev_hash, row_hash, hash, created_at) VALUES "
            "(:id, 't', 'gl', 'gl_posted', 'ledger', 'x', '{}', '', '', '', :ts)"
        ),
        {"id": uuid.uuid4().hex, "ts": datetime(2999, 1, 1, tzinfo=UTC).isoformat()},
    )
    db_session.commit()
    second = log_audit_event_sync(
        db_session, tenant_id="t", user_id="u", action="b", entity_type="job", entity_id="2"
    )
    db_session.commit()
    assert second.prev_hash == first


def test_created_at_never_sorts_before_the_tip(db_session, monkeypatch):
    """A clock that steps backwards must not put a row before its predecessor
    in created_at order, which is the order the verifier walks."""
    import gdx_dispatch.core.audit as audit

    first = log_audit_event_sync(db_session, tenant_id="t", user_id="u", action="a", entity_type="job", entity_id="1")
    db_session.commit()
    monkeypatch.setattr(audit, "utcnow", lambda: datetime(2000, 1, 1, tzinfo=UTC))
    second = log_audit_event_sync(db_session, tenant_id="t", user_id="u", action="b", entity_type="job", entity_id="2")
    db_session.commit()
    def aware(dt):
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    assert aware(second.created_at) > aware(first.created_at)
    assert verify_audit_chain(db_session) is True


# ---------------------------------------------------------------------------
# Verifier: a fork is checked apart from unchained rows.
# ---------------------------------------------------------------------------


def _insert_raw(db, *, prev_hash: str, row_hash: str, entity_id: str, ts: datetime | None = None) -> None:
    db.execute(
        text(
            "INSERT INTO audit_logs (id, tenant_id, user_id, action, entity_type, entity_id, details, "
            "prev_hash, row_hash, hash, created_at) VALUES "
            "(:id, 't', 'x', 'raw', 'job', :eid, '{}', :prev, :row, :row, :ts)"
        ),
        {"id": uuid.uuid4().hex, "eid": entity_id, "prev": prev_hash, "row": row_hash,
         "ts": (ts or datetime.now(UTC)).isoformat()},
    )


def _fork_with_unchained_rows(db) -> str:
    """A real fork (two chained rows naming one parent) plus a GL-style
    unchained row — the prod shape the old verifier hid behind a WARN."""
    parent = log_audit_event_sync(db, tenant_id="t", user_id="u", action="a", entity_type="job", entity_id="p")
    db.commit()
    _insert_raw(db, prev_hash=parent.row_hash, row_hash="a" * 64, entity_id="c1")
    _insert_raw(db, prev_hash=parent.row_hash, row_hash="b" * 64, entity_id="c2")
    _insert_raw(db, prev_hash="", row_hash="", entity_id="gl")
    db.commit()
    return parent.row_hash


def test_report_names_the_fork_apart_from_unchained_rows(db_session):
    parent = _fork_with_unchained_rows(db_session)
    report = audit_chain_report(db_session)
    assert [f["prev_hash"] for f in report["forks"]] == [parent]
    assert report["forks"][0]["children"] == 2
    assert report["unchained"] == 1
    assert report["broken"] is True  # no serialized-writer marker: nothing is history
    assert report["known_breaks_before"] is None


def _write_marker(db) -> AuditLog:
    """The row migration 122 writes."""
    from gdx_dispatch.core.audit import AUDIT_CHAIN_SERIALIZED_ACTION

    return log_audit_event_sync(
        db, user_id="migration:122_audit_chain_serialized", action=AUDIT_CHAIN_SERIALIZED_ACTION,
        entity_type="audit_logs", entity_id="chain",
    )


def _nightly(db_session, monkeypatch):
    import gdx_dispatch.tasks.audit_chain_verify as mod

    class _Ctx:
        def __enter__(self):
            return db_session

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "SessionLocal", lambda: _Ctx())
    return mod


def test_nightly_alerts_on_a_fork_even_with_unchained_rows(db_session, monkeypatch, caplog):
    mod = _nightly(db_session, monkeypatch)
    _fork_with_unchained_rows(db_session)
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        out = mod.verify_chain_nightly()
    assert out["unchained"] == 1
    assert out["forks"] == 1
    broken = [r for r in caplog.records if "AUDIT_CHAIN_BROKEN" in r.getMessage()]
    assert len(broken) == 1 and broken[0].levelno == logging.ERROR
    assert getattr(broken[0], "ops_alert", False) is True


def test_nightly_unchained_rows_alone_only_warn(db_session, monkeypatch, caplog):
    mod = _nightly(db_session, monkeypatch)
    log_audit_event_sync(db_session, tenant_id="t", user_id="u", action="a", entity_type="job", entity_id="1")
    _insert_raw(db_session, prev_hash="", row_hash="", entity_id="gl")
    db_session.commit()
    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        out = mod.verify_chain_nightly()
    assert out["forks"] == 0
    assert not [r for r in caplog.records if "AUDIT_CHAIN_BROKEN" in r.getMessage()]
    assert [r for r in caplog.records if "audit_chain_has_unchained_rows" in r.getMessage()]


def test_nightly_known_forks_are_history_but_a_new_one_alerts(db_session, monkeypatch, caplog):
    """Forks older than the serialized-writer marker are history: counted, not
    paged. A fork written after it still alerts, beside them."""
    import gdx_dispatch.core.audit as audit

    mod = _nightly(db_session, monkeypatch)
    old = datetime(2025, 6, 1, tzinfo=UTC)
    _write_marker(db_session)
    parent = log_audit_event_sync(db_session, tenant_id="t", user_id="u", action="a", entity_type="job", entity_id="p")
    _insert_raw(db_session, prev_hash=parent.row_hash, row_hash="a" * 64, entity_id="c1", ts=old)
    _insert_raw(db_session, prev_hash=parent.row_hash, row_hash="b" * 64, entity_id="c2", ts=old)
    db_session.commit()
    # Raw rows carry made-up hashes; this test is about forks, not hashes.
    real_report = audit.audit_chain_report

    def forks_only(db):
        r = real_report(db)
        r["bad_hash"] = 0
        r["broken"] = bool(r["dangling"] or r["new_forks"] or r["new_restarts"])
        return r

    monkeypatch.setattr(mod, "audit_chain_report", forks_only)
    with caplog.at_level(logging.INFO, logger=mod.__name__):
        out = mod.verify_chain_nightly()
    assert out["forks"] == 1 and out["new_forks"] == 0 and out["broken"] is False
    assert not [r for r in caplog.records if "AUDIT_CHAIN_BROKEN" in r.getMessage()]
    assert [r for r in caplog.records if "audit_chain_known_breaks" in r.getMessage()]

    caplog.clear()
    _insert_raw(db_session, prev_hash="a" * 64, row_hash="c" * 64, entity_id="d1")
    _insert_raw(db_session, prev_hash="a" * 64, row_hash="d" * 64, entity_id="d2")
    db_session.commit()
    with caplog.at_level(logging.INFO, logger=mod.__name__):
        out = mod.verify_chain_nightly()
    assert out["new_forks"] == 1 and out["broken"] is True
    assert [r for r in caplog.records if "AUDIT_CHAIN_BROKEN" in r.getMessage()]


def test_report_flags_a_row_whose_hash_does_not_match(db_session):
    log_audit_event_sync(db_session, tenant_id="t", user_id="u", action="a", entity_type="job", entity_id="1")
    db_session.commit()
    _insert_raw(db_session, prev_hash="", row_hash="e" * 64, entity_id="forged")
    db_session.commit()
    report = audit_chain_report(db_session)
    assert report["bad_hash"] == 1
    assert report["broken"] is True


def test_endpoint_reports_a_fork_as_tamper_despite_unchained_rows(db_session):
    from gdx_dispatch.routers.audit import verify_audit_chain_endpoint

    _fork_with_unchained_rows(db_session)
    res = verify_audit_chain_endpoint(entity_type=None, entity_id=None, _={"role": "admin"}, db=db_session)
    assert res["ok"] is False
    assert res["unchained_rows"] == 1
    assert res["forked_parents"] == 1
    assert res["new_forks"] == 1
    assert res["tamper_suspected"] is True
