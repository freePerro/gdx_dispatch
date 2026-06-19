"""SS-28 slice E tests — audit retention cron.

Covers:
* Default 90-day retention applied when no AuditRetentionPolicy row
* Per-tenant policy override respected
* Current-month safety floor: rows inside current month NEVER pruned,
  even when policy would otherwise eat them
* Dry-run does not delete
* Batched delete: 2500 rows (> BATCH_SIZE) all pruned in multiple passes
* Idempotent: second run deletes nothing
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.models.platform_ss28_additions import (
    AuditRetentionPolicy,
    SS28Base,
    PlatformConsumerAudit,
)
from gdx_dispatch.tools.audit_retention_cron import (
    DEFAULT_RETENTION_DAYS,
    _compute_cutoff,
    _start_of_current_month,
    prune_audit_rows,
)
from uuid import UUID

T1 = UUID("00000000-0000-0000-0000-000000000001")
T2 = UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    SS28Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _insert_raw(db, tenant_id, created_at, action="a", pid="p"):
    """Insert a row bypassing record_consumer_action (we need to plant
    rows with arbitrary created_at values for retention testing)."""
    from uuid import uuid4

    # Hashes are irrelevant here — retention cron does not re-verify the
    # chain before pruning. Use placeholder 64-char hex strings.
    row = PlatformConsumerAudit(
        id=uuid4(),
        tenant_id=tenant_id,
        principal_identity_id=pid,
        action=action,
        resource_type="x",
        resource_id="1",
        result="ok",
        details=None,
        ip_address=None,
        user_agent=None,
        created_at=created_at,
        prev_hash="0" * 64,
        row_hash="1" * 64,
    )
    db.add(row)


def test_current_month_safety_floor(db):
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    # Policy says 7 days — would normally eat rows from 2026-04-12 back.
    db.add(AuditRetentionPolicy(tenant_id=T1, retention_days=7))
    # Row from early in current month — MUST be preserved.
    _insert_raw(db, T1, datetime(2026, 4, 2, tzinfo=timezone.utc))
    # Row from previous month — should be pruned.
    _insert_raw(db, T1, datetime(2026, 3, 15, tzinfo=timezone.utc))
    db.commit()

    results = prune_audit_rows(db, now=now)
    assert len(results) == 1
    assert results[0].deleted == 1

    remaining = db.query(PlatformConsumerAudit).filter_by(tenant_id=T1).all()
    assert len(remaining) == 1
    assert remaining[0].created_at.month == 4


def test_default_retention_applied(db):
    now = datetime(2026, 4, 19, tzinfo=timezone.utc)
    # No AuditRetentionPolicy row → default 90d. 91d-old row past cutoff
    # AND past current-month floor → should be pruned.
    _insert_raw(db, T1, now - timedelta(days=200))
    # 30d-old row — inside 90d window → keep.
    _insert_raw(db, T1, now - timedelta(days=30))
    db.commit()

    results = prune_audit_rows(db, now=now)
    assert results[0].deleted == 1
    assert db.query(PlatformConsumerAudit).count() == 1


def test_dry_run_does_not_delete(db):
    now = datetime(2026, 4, 19, tzinfo=timezone.utc)
    _insert_raw(db, T1, now - timedelta(days=200))
    db.commit()

    results = prune_audit_rows(db, now=now, dry_run=True)
    assert results[0].candidates == 1
    assert results[0].deleted == 0
    assert db.query(PlatformConsumerAudit).count() == 1


def test_idempotent(db):
    now = datetime(2026, 4, 19, tzinfo=timezone.utc)
    _insert_raw(db, T1, now - timedelta(days=200))
    db.commit()

    r1 = prune_audit_rows(db, now=now)
    r2 = prune_audit_rows(db, now=now)
    assert r1[0].deleted == 1
    # Second run: tenant may no longer appear (no rows) → empty list.
    assert all(r.deleted == 0 for r in r2)


def test_batched_delete_handles_large_set(db):
    now = datetime(2026, 4, 19, tzinfo=timezone.utc)
    old_ts = now - timedelta(days=365)
    for i in range(2500):
        _insert_raw(db, T1, old_ts + timedelta(seconds=i))
    db.commit()

    results = prune_audit_rows(db, now=now)
    assert results[0].deleted == 2500
    assert db.query(PlatformConsumerAudit).count() == 0


def test_per_tenant_retention_independent(db):
    now = datetime(2026, 4, 19, tzinfo=timezone.utc)
    db.add(AuditRetentionPolicy(tenant_id=T1, retention_days=30))
    db.add(AuditRetentionPolicy(tenant_id=T2, retention_days=365))
    _insert_raw(db, T1, now - timedelta(days=200))
    _insert_raw(db, T2, now - timedelta(days=200))
    db.commit()

    prune_audit_rows(db, now=now)

    assert db.query(PlatformConsumerAudit).filter_by(tenant_id=T1).count() == 0
    assert db.query(PlatformConsumerAudit).filter_by(tenant_id=T2).count() == 1


def test_compute_cutoff_floors_to_start_of_month():
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    # 5-day retention would produce 2026-04-14 — inside current month.
    # Must floor to 2026-04-01.
    cutoff = _compute_cutoff(now, 5)
    assert cutoff == _start_of_current_month(now)


def test_default_retention_constant_is_90():
    # Documented guarantee — tests pin this so a silent drop to, say,
    # 30 days can't ship without an explicit test update.
    assert DEFAULT_RETENTION_DAYS == 90
