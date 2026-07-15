"""GL Phase 1 (S3) — engine ↔ trigger integration on real Postgres.

The SQLite suite (test_gl_engine.py) proves the engine's logic; this proves
the engine's output SURVIVES the S1 integrity triggers on PG: the sealing
trigger demands ``created_txid = txid_current()`` on line insert (the engine
must set it, in the same transaction), and the deferred balance trigger fires
at COMMIT. Same self-provisioning pattern as test_gl_triggers.py — throwaway
PG database per test, skips if PG is unreachable.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.modules.ledger.coa import seed_coa
from gdx_dispatch.modules.ledger.ddl import install_gl_triggers
from gdx_dispatch.modules.ledger.engine import (
    PostingEvent,
    PostingLine,
    post_for_event,
    reverse_entry,
)
from gdx_dispatch.modules.ledger.models import (
    GlAccount,
    GlJournalEntry,
    GlJournalLine,
    GlPeriodLock,
    GlSettings,
)

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT  # noqa: E402

PG_HOST = os.environ.get("GDX_TEST_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("GDX_TEST_PG_PORT", "5433"))
PG_USER = os.environ.get("GDX_TEST_PG_USER", "gdx")
PG_PASSWORD = os.environ.get("GDX_TEST_PG_PASSWORD", "gdx")
PG_ADMIN_DB = os.environ.get("GDX_TEST_PG_ADMIN_DB", "gdx")

COMPANY = "11111111-1111-1111-1111-111111111111"
DAY = dt.date(2026, 7, 1)

_GL_TABLES = [
    GlAccount.__table__,
    GlJournalEntry.__table__,
    GlJournalLine.__table__,
    GlPeriodLock.__table__,
    GlSettings.__table__,
    AuditLog.__table__,  # the lock-override path writes an audit row
]


@pytest.fixture
def pg_ledger():
    try:
        admin = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=PG_ADMIN_DB
        )
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable at {PG_HOST}:{PG_PORT}: {exc}")
    admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)

    dbname = f"gl_eng_{uuid.uuid4().hex[:12]}"
    cur = admin.cursor()
    cur.execute(f'CREATE DATABASE "{dbname}"')
    cur.close()

    url = f"postgresql+psycopg2://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{dbname}"
    engine = create_engine(url, future=True)
    GlAccount.metadata.create_all(engine, tables=_GL_TABLES)
    with engine.begin() as conn:
        install_gl_triggers(conn)

    session = Session(engine, future=True)
    seed_coa(session, COMPANY)
    session.commit()

    yield session

    session.close()
    engine.dispose()
    cur = admin.cursor()
    cur.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')
    cur.close()
    admin.close()


def _event(amount=10_000, source_id="inv-1"):
    return PostingEvent(
        company_id=COMPANY,
        source_type="invoice",
        source_id=source_id,
        event="issued",
        effective_at=DAY,
        lines=(
            PostingLine(amount_cents=amount, role="AR"),
            PostingLine(amount_cents=-amount, role="SALES_FALLBACK"),
        ),
    )


def test_engine_posting_survives_triggers_at_commit(pg_ledger):
    """Sealing wants created_txid = txid_current(); balance is a DEFERRED
    constraint trigger — the real proof is a clean COMMIT."""
    entry = post_for_event(pg_ledger, _event())
    pg_ledger.commit()

    fetched = pg_ledger.get(GlJournalEntry, entry.id)
    assert fetched.status == "posted"
    assert fetched.created_txid is not None
    assert fetched.entry_no >= 1  # PG sequence fired
    lines = pg_ledger.scalars(
        select(GlJournalLine).where(GlJournalLine.entry_id == entry.id)
    ).all()
    assert sorted(l.amount_cents for l in lines) == [-10_000, 10_000]


def test_engine_reversal_survives_triggers_at_commit(pg_ledger):
    """The reversal updates the original's status/reversed_by — exactly the
    two transitions the immutability trigger permits — and inserts mirrored
    lines under a fresh txid seal."""
    entry = post_for_event(pg_ledger, _event())
    pg_ledger.commit()

    reversal = reverse_entry(pg_ledger, entry)
    pg_ledger.commit()

    assert pg_ledger.get(GlJournalEntry, entry.id).status == "reversed"
    assert pg_ledger.get(GlJournalEntry, reversal.id).reverses_entry_id == entry.id


def test_idempotent_replay_across_committed_transactions(pg_ledger):
    first = post_for_event(pg_ledger, _event())
    pg_ledger.commit()
    again = post_for_event(pg_ledger, _event())
    pg_ledger.commit()
    assert again.id == first.id
    assert pg_ledger.scalar(text("SELECT count(*) FROM gl_journal_entries")) == 1


def test_savepoint_collision_does_not_abort_enclosing_transaction(pg_ledger, monkeypatch):
    """§5.6: the insert runs inside a SAVEPOINT so a key collision must not
    poison the caller's transaction. Blind the pre-flight check once so the
    INSERT genuinely violates the unique index on PG — then prove the outer
    transaction is still usable (a naive raw INSERT would have left it in
    'current transaction is aborted' state)."""
    import gdx_dispatch.modules.ledger.engine as engine_mod

    first = post_for_event(pg_ledger, _event())
    pg_ledger.commit()

    real = engine_mod._entry_by_key
    calls = {"n": 0}

    def blind_once(session, company_id, key):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real(session, company_id, key)

    monkeypatch.setattr(engine_mod, "_entry_by_key", blind_once)
    again = post_for_event(pg_ledger, _event())
    assert again.id == first.id
    assert calls["n"] >= 2  # IntegrityError branch executed for real

    # the enclosing transaction survived the savepoint rollback on PG
    other = post_for_event(pg_ledger, _event(source_id="inv-2"))
    pg_ledger.commit()
    assert pg_ledger.get(GlJournalEntry, other.id).status == "posted"


def test_lock_override_audit_logs_on_pg(pg_ledger):
    """The accounting.close override writes an AuditLog row — proven on PG
    (the table ships in the fixture precisely because this path needs it)."""
    from gdx_dispatch.modules.ledger.engine import PeriodLockedError

    pg_ledger.add(GlPeriodLock(lock_date=DAY, company_id=COMPANY))
    pg_ledger.flush()

    with pytest.raises(PeriodLockedError):
        post_for_event(pg_ledger, _event())

    entry = post_for_event(
        pg_ledger,
        PostingEvent(
            company_id=COMPANY, source_type="invoice", source_id="inv-1",
            event="issued", effective_at=DAY, created_by="closer",
            lines=(
                PostingLine(amount_cents=100, role="AR"),
                PostingLine(amount_cents=-100, role="SALES_FALLBACK"),
            ),
        ),
        override_lock=True,
    )
    pg_ledger.commit()
    assert pg_ledger.get(GlJournalEntry, entry.id).status == "posted"
    log = pg_ledger.scalars(
        select(AuditLog).where(AuditLog.action == "gl_posted_into_locked_period")
    ).one()
    assert log.user_id == "closer"
