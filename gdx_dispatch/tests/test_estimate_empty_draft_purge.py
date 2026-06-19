"""S-autosave slice 5 — _purge_empty_drafts_for_tenant.

Hard-deletes empty drafts older than threshold. Empty = zero lines AND
sent_at IS NULL AND created_at < cutoff. Anything else is preserved.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.tasks.estimate_archive import _purge_empty_drafts_for_tenant


def _setup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return Session


def _add(Session, *, status="draft", days_old=10, with_line=False, sent=False):
    db = Session()
    try:
        created = datetime.now(timezone.utc) - timedelta(days=days_old)
        est = Estimate(
            estimate_number=f"E-{uuid4().hex[:8]}",
            status=status,
            company_id="tenant-test",
            public_token=uuid4().hex,
            created_at=created,
            updated_at=created,
            sent_at=created if sent else None,
            total=Decimal("0.00"),
        )
        db.add(est)
        db.flush()
        if with_line:
            db.add(EstimateLine(
                estimate_id=est.id,
                description="line",
                quantity=1,
                unit_price=Decimal("100"),
                line_total=Decimal("100"),
                company_id="tenant-test",
            ))
        db.commit()
        return est.id
    finally:
        db.close()


def _ids(Session) -> set:
    db = Session()
    try:
        return {row.id for row in db.execute(select(Estimate)).scalars().all()}
    finally:
        db.close()


def test_purges_empty_old_drafts(tmp_path):
    Session = _setup(tmp_path)

    keep_with_line = _add(Session, days_old=30, with_line=True)
    keep_recent = _add(Session, days_old=2, with_line=False)
    keep_sent = _add(Session, days_old=30, with_line=False, sent=True)
    keep_non_draft = _add(Session, status="sent", days_old=30, with_line=False)
    purge_target = _add(Session, days_old=30, with_line=False)

    assert len(_ids(Session)) == 5
    with patch("gdx_dispatch.tasks.estimate_archive.SessionLocal", Session):
        purged = _purge_empty_drafts_for_tenant("tenant-test", threshold_days=7)
    assert purged == 1

    remaining = _ids(Session)
    assert purge_target not in remaining
    assert {keep_with_line, keep_recent, keep_sent, keep_non_draft} <= remaining


def test_threshold_zero_disables(tmp_path):
    Session = _setup(tmp_path)
    _add(Session, days_old=999, with_line=False)
    with patch("gdx_dispatch.tasks.estimate_archive.SessionLocal", Session):
        purged = _purge_empty_drafts_for_tenant("tenant-test", threshold_days=0)
    assert purged == 0
    assert len(_ids(Session)) == 1
