"""Timeclock stuck-shift sweep — first tests for a task that shipped dead.

The sweep never ran until v1.10.1 registered it (2026-07-08 audit batch);
its FIRST prod execution then failed inside the audit INSERT:
`:details::jsonb` reached Postgres as a literal `:details` (syntax
error), the row_hash/prev_hash NOT NULL hash-chain columns were omitted,
and the single wrapping transaction rolled the shift close back with it —
the 66-day stuck shift stayed open. These tests run the REAL close path
(no mocked SQL) against sqlite: stale shift closes with sane minutes, a
fresh shift is untouched, and the audit row lands through the canonical
hash-chained writer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase, verify_audit_chain
from gdx_dispatch.models.tenant_models import TimeclockEntry
from gdx_dispatch.routers.timeclock import MAX_SHIFT_HOURS
from gdx_dispatch.tasks.timeclock_sweep import _close_stale_for_tenant

TENANT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


def _entry(session_factory, *, hours_ago: float) -> str:
    now = datetime.now(timezone.utc)
    entry_id = str(uuid4())
    db = session_factory()
    db.add(
        TimeclockEntry(
            id=entry_id,
            tenant_id=TENANT,
            technician_id=str(uuid4()),
            clock_in_at=(now - timedelta(hours=hours_ago)).isoformat(),
            entry_type="shift",
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
    )
    db.commit()
    db.close()
    return entry_id


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with patch("gdx_dispatch.tasks.timeclock_sweep.SessionLocal", factory):
        yield factory
    engine.dispose()


def test_sweep_closes_stale_shift_and_audits(session_factory):
    stale_id = _entry(session_factory, hours_ago=66 * 24)  # the prod shift
    fresh_id = _entry(session_factory, hours_ago=2)

    result = _close_stale_for_tenant(TENANT)
    assert result == {"closed": 1, "failures": 0}

    db = session_factory()
    stale = db.get(TimeclockEntry, stale_id)
    fresh = db.get(TimeclockEntry, fresh_id)
    assert stale.clock_out_at is not None
    expected_minutes = 66 * 24 * 60
    assert abs(stale.minutes - expected_minutes) <= 2
    assert fresh.clock_out_at is None  # under the 16h cap — untouched

    audit = db.execute(
        select(AuditLog).where(AuditLog.action == "timeclock_auto_close")
    ).scalar_one()
    assert audit.entity_id == stale_id
    assert audit.user_id == "system"
    assert audit.details["reason"] == "sweep_max_shift"
    assert audit.details["max_shift_hours"] == MAX_SHIFT_HOURS
    # The canonical writer must keep the hash chain intact — the raw-SQL
    # version inserted no hashes at all.
    assert audit.row_hash and verify_audit_chain(db) is True
    db.close()


def test_sweep_noop_when_nothing_stale(session_factory):
    _entry(session_factory, hours_ago=1)
    assert _close_stale_for_tenant(TENANT) == {"closed": 0, "failures": 0}
