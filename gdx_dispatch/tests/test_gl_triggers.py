"""GL Phase 1 (S1) — Postgres integrity trigger tests.

Self-provisioning: creates a throwaway PG database, ``create_all``s just the
``gl_*`` tables (they carry no cross-domain FKs, so they stand alone), and
installs the migration-012 triggers via the shared
``modules.ledger.ddl.install_gl_triggers`` — so this file does NOT depend on
tests/fixtures/structure.sql. Skips automatically when no test PostgreSQL is
reachable. Connection target = the same GDX_TEST_PG_* env the pg harness uses
(default 127.0.0.1:5433, user/pw gdx/gdx — the gdx-test-postgres container).

Covers: balance invariant (deferred), immutability (line UPDATE/DELETE, entry
DELETE, non-exempt entry UPDATE), the two exempt entry transitions, and sealing
(cross-transaction line insert, and missing created_txid).
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from gdx_dispatch.modules.ledger.ddl import install_gl_triggers
from gdx_dispatch.modules.ledger.models import (
    GlAccount,
    GlJournalEntry,
    GlJournalLine,
    GlPeriodLock,
)

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT  # noqa: E402

PG_HOST = os.environ.get("GDX_TEST_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("GDX_TEST_PG_PORT", "5433"))
PG_USER = os.environ.get("GDX_TEST_PG_USER", "gdx")
PG_PASSWORD = os.environ.get("GDX_TEST_PG_PASSWORD", "gdx")
PG_ADMIN_DB = os.environ.get("GDX_TEST_PG_ADMIN_DB", "gdx")

COMPANY = "11111111-1111-1111-1111-111111111111"
CASH = str(uuid.uuid4())
AR = str(uuid.uuid4())

_GL_TABLES = [
    GlAccount.__table__,
    GlJournalEntry.__table__,
    GlJournalLine.__table__,
    GlPeriodLock.__table__,
]


def _admin_conn():
    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=PG_ADMIN_DB
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


@pytest.fixture
def gl():
    """Throwaway PG DB with only the gl_* tables + triggers. Yields a Session."""
    try:
        admin = _admin_conn()
    except psycopg2.OperationalError as exc:
        pytest.skip(
            f"PostgreSQL not reachable at {PG_HOST}:{PG_PORT} "
            f"(set GDX_TEST_PG_* to run GL trigger tests): {exc}"
        )

    dbname = f"gl_trig_{uuid.uuid4().hex[:12]}"
    cur = admin.cursor()
    cur.execute(f'CREATE DATABASE "{dbname}"')
    cur.close()

    url = f"postgresql+psycopg2://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{dbname}"
    engine = create_engine(url, future=True)
    # Only the gl_* tables — they carry no FK to operational tables.
    GlAccount.metadata.create_all(engine, tables=_GL_TABLES)
    with engine.begin() as conn:
        install_gl_triggers(conn)

    session = Session(engine, future=True)
    session.execute(
        text(
            "INSERT INTO gl_accounts (id, code, name, type, is_system, active, company_id, created_at) "
            "VALUES (:cash,'1000','Operating Bank','asset',true,true,:co,now()), "
            "       (:ar,'1200','Accounts Receivable','asset',true,true,:co,now())"
        ),
        {"cash": CASH, "ar": AR, "co": COMPANY},
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        cur = admin.cursor()
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (dbname,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        cur.close()
        admin.close()


def _entry(session, *, status="posted", with_txid=True) -> str:
    txid = "txid_current()" if with_txid else "NULL"
    return session.execute(
        text(
            "INSERT INTO gl_journal_entries "
            "(id, entry_no, effective_at, posted_at, status, created_txid, company_id, created_at) "
            f"VALUES (gen_random_uuid(), nextval('gl_journal_entry_no_seq'), CURRENT_DATE, now(), "
            f":status, {txid}, :co, now()) RETURNING id"
        ),
        {"status": status, "co": COMPANY},
    ).scalar()


def _line(session, entry_id, account_id, amount) -> str:
    return session.execute(
        text(
            "INSERT INTO gl_journal_lines (id, entry_id, account_id, amount_cents, created_at) "
            "VALUES (gen_random_uuid(), :eid, :acct, :amt, now()) RETURNING id"
        ),
        {"eid": entry_id, "acct": account_id, "amt": amount},
    ).scalar()


def _balanced_entry(session) -> str:
    """Insert a committed, balanced entry (debit cash 5000 / credit AR 5000)."""
    eid = _entry(session)
    _line(session, eid, CASH, 5000)
    _line(session, eid, AR, -5000)
    session.commit()
    return eid


# ── balance invariant (deferred to commit) ──────────────────────────────────

def test_balanced_entry_commits(gl):
    eid = _balanced_entry(gl)
    total = gl.execute(
        text("SELECT SUM(amount_cents) FROM gl_journal_lines WHERE entry_id = :e"), {"e": eid}
    ).scalar()
    assert total == 0


def test_unbalanced_entry_rejected_at_commit(gl):
    eid = _entry(gl)
    _line(gl, eid, CASH, 5000)
    _line(gl, eid, AR, -4000)  # sum = 1000 ≠ 0
    with pytest.raises(DBAPIError) as exc:
        gl.commit()
    assert "unbalanced" in str(exc.value).lower()


def test_single_line_entry_rejected_at_commit(gl):
    eid = _entry(gl)
    _line(gl, eid, CASH, 5000)  # one line → sum ≠ 0 and count < 2
    with pytest.raises(DBAPIError):
        gl.commit()


# ── immutability ─────────────────────────────────────────────────────────────

def test_line_update_rejected(gl):
    eid = _balanced_entry(gl)
    with pytest.raises(DBAPIError) as exc:
        gl.execute(text("UPDATE gl_journal_lines SET amount_cents = 999 WHERE entry_id = :e"), {"e": eid})
    assert "append-only" in str(exc.value).lower()


def test_line_delete_rejected(gl):
    eid = _balanced_entry(gl)
    with pytest.raises(DBAPIError) as exc:
        gl.execute(text("DELETE FROM gl_journal_lines WHERE entry_id = :e"), {"e": eid})
    assert "append-only" in str(exc.value).lower()


def test_entry_delete_rejected(gl):
    eid = _balanced_entry(gl)
    with pytest.raises(DBAPIError) as exc:
        gl.execute(text("DELETE FROM gl_journal_entries WHERE id = :e"), {"e": eid})
    assert "append-only" in str(exc.value).lower()


def test_entry_non_exempt_update_rejected(gl):
    eid = _balanced_entry(gl)
    with pytest.raises(DBAPIError) as exc:
        gl.execute(text("UPDATE gl_journal_entries SET source_type = 'tampered' WHERE id = :e"), {"e": eid})
    assert "append-only" in str(exc.value).lower()


# ── exempt entry transitions ─────────────────────────────────────────────────

def test_entry_reversal_transition_allowed(gl):
    original = _balanced_entry(gl)
    reversing = _balanced_entry(gl)
    # posted -> reversed AND reversed_by_entry_id NULL -> value, in one UPDATE.
    gl.execute(
        text(
            "UPDATE gl_journal_entries SET status = 'reversed', reversed_by_entry_id = :rev WHERE id = :e"
        ),
        {"rev": reversing, "e": original},
    )
    gl.commit()
    status = gl.execute(
        text("SELECT status FROM gl_journal_entries WHERE id = :e"), {"e": original}
    ).scalar()
    assert status == "reversed"


def test_entry_bad_status_transition_rejected(gl):
    # An entry that starts reversed cannot be un-reversed (reversed -> posted).
    eid = _entry(gl, status="reversed")
    _line(gl, eid, CASH, 5000)
    _line(gl, eid, AR, -5000)
    gl.commit()
    with pytest.raises(DBAPIError) as exc:
        gl.execute(text("UPDATE gl_journal_entries SET status = 'posted' WHERE id = :e"), {"e": eid})
    assert "posted->reversed" in str(exc.value).lower() or "append-only" in str(exc.value).lower()


def test_entry_reversed_by_is_write_once(gl):
    original = _balanced_entry(gl)
    rev1 = _balanced_entry(gl)
    rev2 = _balanced_entry(gl)
    gl.execute(
        text("UPDATE gl_journal_entries SET status='reversed', reversed_by_entry_id = :r WHERE id = :e"),
        {"r": rev1, "e": original},
    )
    gl.commit()
    with pytest.raises(DBAPIError) as exc:
        gl.execute(
            text("UPDATE gl_journal_entries SET reversed_by_entry_id = :r WHERE id = :e"),
            {"r": rev2, "e": original},
        )
    assert "write-once" in str(exc.value).lower()


# ── sealing (cross-transaction line insert) ──────────────────────────────────

def test_sealing_rejects_cross_transaction_line(gl):
    eid = _balanced_entry(gl)  # committed in its own transaction
    # New transaction now — inserting another line for the sealed entry is rejected
    # by the BEFORE INSERT sealing trigger before the balance check even runs.
    with pytest.raises(DBAPIError) as exc:
        _line(gl, eid, CASH, 1)
    assert "sealed" in str(exc.value).lower()


def test_sealing_rejects_null_created_txid(gl):
    eid = _entry(gl, with_txid=False)  # created_txid = NULL
    with pytest.raises(DBAPIError) as exc:
        _line(gl, eid, CASH, 5000)
    assert "created_txid" in str(exc.value).lower()
