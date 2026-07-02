"""GL Phase 1 (S1) — ORM/model smoke tests (SQLite; no triggers).

These run in the normal suite and verify the models register, ``create_all``
builds the ``gl_*`` tables, a balanced entry round-trips, and the
``amount_cents <> 0`` CHECK is enforced (SQLite honours CHECK constraints). The
Postgres-only integrity triggers are covered by ``test_gl_triggers.py``.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from gdx_dispatch.modules.ledger.models import (
    ROLE_AR,
    GlAccount,
    GlJournalEntry,
    GlJournalLine,
    GlPeriodLock,
)

COMPANY = "11111111-1111-1111-1111-111111111111"


def _account(**kw) -> GlAccount:
    kw.setdefault("code", "1200")
    kw.setdefault("name", "Accounts Receivable")
    kw.setdefault("type", "asset")
    kw.setdefault("company_id", COMPANY)
    return GlAccount(**kw)


def test_gl_tables_created(tenant_db):
    """create_all built every gl_* table (models registered on the metadata)."""
    tables = set(inspect(tenant_db.get_bind()).get_table_names())
    assert {
        "gl_accounts",
        "gl_journal_entries",
        "gl_journal_lines",
        "gl_period_locks",
    } <= tables


def test_account_role_roundtrip(tenant_db):
    acct = _account(role=ROLE_AR, is_system=True)
    tenant_db.add(acct)
    tenant_db.commit()
    fetched = tenant_db.get(GlAccount, acct.id)
    assert fetched.role == ROLE_AR
    assert fetched.is_system is True
    assert fetched.active is True  # default


def test_balanced_entry_roundtrips(tenant_db):
    cash = _account(code="1000", name="Operating Bank", role="OPERATING_BANK", is_system=True)
    ar = _account(role=ROLE_AR, is_system=True)
    tenant_db.add_all([cash, ar])
    tenant_db.flush()

    entry = GlJournalEntry(
        entry_no=1,
        effective_at=dt.date(2026, 7, 2),
        source_type="payment",
        source_id=str(uuid.uuid4()),
        company_id=COMPANY,
    )
    tenant_db.add(entry)
    tenant_db.flush()
    # Debit cash +5000, credit AR -5000 → balanced.
    tenant_db.add_all([
        GlJournalLine(entry_id=entry.id, account_id=cash.id, amount_cents=5000),
        GlJournalLine(entry_id=entry.id, account_id=ar.id, amount_cents=-5000),
    ])
    tenant_db.commit()

    lines = tenant_db.query(GlJournalLine).filter_by(entry_id=entry.id).all()
    assert len(lines) == 2
    assert sum(line.amount_cents for line in lines) == 0
    assert entry.status == "posted"  # born posted


def test_zero_amount_line_rejected(tenant_db):
    """The amount_cents <> 0 CHECK is enforced even without triggers (SQLite)."""
    ar = _account(role=ROLE_AR, is_system=True)
    tenant_db.add(ar)
    tenant_db.flush()
    entry = GlJournalEntry(entry_no=2, effective_at=dt.date(2026, 7, 2), company_id=COMPANY)
    tenant_db.add(entry)
    tenant_db.flush()
    tenant_db.add(GlJournalLine(entry_id=entry.id, account_id=ar.id, amount_cents=0))
    with pytest.raises(IntegrityError):
        tenant_db.commit()


def test_period_lock_roundtrips(tenant_db):
    lock = GlPeriodLock(lock_date=dt.date(2026, 6, 30), note="June close", company_id=COMPANY)
    tenant_db.add(lock)
    tenant_db.commit()
    assert tenant_db.query(GlPeriodLock).count() == 1
