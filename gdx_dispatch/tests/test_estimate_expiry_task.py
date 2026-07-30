"""Nightly estimate-expiry task — plan §15.

The task marks still-'sent' estimates past their valid_until as 'expired'.
Draft/accepted/declined and future-dated sent estimates are left alone.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models.tenant_models  # noqa: F401 — registers Job etc. so the estimates FK resolves
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.proposals.models import Estimate


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    engine.dispose()


_counter = iter(range(1, 10000))


def _mk(db, *, status, valid_until):
    est = Estimate(
        id=uuid.uuid4(),
        estimate_number=f"EST-TEST-{next(_counter):04d}",
        public_token=uuid.uuid4().hex,
        status=status,
        valid_until=valid_until,
        company_id="tenant-test",
        created_at=datetime.now(timezone.utc),
    )
    db.add(est)
    return est


def test_expire_stale_nightly_only_expires_past_due_sent(session_factory, monkeypatch):
    from gdx_dispatch.tasks import estimate_expiry

    monkeypatch.setattr(estimate_expiry, "SessionLocal", session_factory)

    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=30)

    db = session_factory()
    stale_sent = _mk(db, status="sent", valid_until=past)          # -> expired
    fresh_sent = _mk(db, status="sent", valid_until=future)        # stays sent
    no_date_sent = _mk(db, status="sent", valid_until=None)        # stays sent
    old_draft = _mk(db, status="draft", valid_until=past)          # drafts untouched
    old_accepted = _mk(db, status="accepted", valid_until=past)    # terminal, untouched
    db.commit()
    ids = {
        "stale_sent": stale_sent.id, "fresh_sent": fresh_sent.id,
        "no_date_sent": no_date_sent.id, "old_draft": old_draft.id,
        "old_accepted": old_accepted.id,
    }
    db.close()

    result = estimate_expiry.expire_stale_nightly()
    assert result["expired_count"] == 1

    check = session_factory()
    try:
        statuses = {
            name: check.query(Estimate).filter(Estimate.id == eid).one().status
            for name, eid in ids.items()
        }
    finally:
        check.close()

    assert statuses["stale_sent"] == "expired"
    assert statuses["fresh_sent"] == "sent"
    assert statuses["no_date_sent"] == "sent"
    assert statuses["old_draft"] == "draft"
    assert statuses["old_accepted"] == "accepted"
