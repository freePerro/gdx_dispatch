"""Wave D / S6 — stats daily roll-up tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.phone_com.models import (
    PhoneComCall,
    PhoneComMessage,
    PhoneComStatsDaily,
    PhoneComVoicemail,
)
from gdx_dispatch.modules.phone_com.stats import (
    _is_missed,
    roll_up_all_history,
    roll_up_recent,
)


@pytest.fixture
def tenant_db():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(e)
    sm = sessionmaker(bind=e, expire_on_commit=False)
    return sm()


def _add_call(db, when, direction="in", status="answered", duration_s=60):
    db.add(PhoneComCall(
        phone_com_call_id=str(uuid4()),
        direction=direction,
        status=status,
        duration_s=duration_s,
        started_at=when,
        raw_payload={},
    ))


def test_is_missed_classifier():
    assert _is_missed("type voicemail_received") is True
    assert _is_missed("missed") is True
    assert _is_missed("no_answer") is True
    assert _is_missed("busy") is True
    assert _is_missed("answered") is False
    assert _is_missed("dial_out +13202325143") is False
    assert _is_missed(None) is False
    assert _is_missed("") is False


def test_roll_up_recent_aggregates_last_7d(tenant_db):
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    older = today - timedelta(days=10)  # outside 7d window

    _add_call(tenant_db, today, direction="in", status="answered", duration_s=120)
    _add_call(tenant_db, today, direction="in", status="type voicemail_received", duration_s=30)
    _add_call(tenant_db, today, direction="out", status="completed", duration_s=180)
    _add_call(tenant_db, yesterday, direction="in", status="missed", duration_s=0)
    _add_call(tenant_db, older, direction="in", status="answered", duration_s=60)
    tenant_db.commit()

    result = roll_up_recent(tenant_db, days=7)
    assert result["days_rolled_up"] == 7

    rows = {r.stat_date: r for r in tenant_db.query(PhoneComStatsDaily).all()}
    today_d = today.date()
    yesterday_d = yesterday.date()

    assert today_d in rows
    assert rows[today_d].calls_in == 2
    assert rows[today_d].calls_out == 1
    assert rows[today_d].calls_missed == 1  # voicemail_received
    assert rows[today_d].total_call_minutes == (120 + 30 + 180) // 60  # 5

    assert yesterday_d in rows
    assert rows[yesterday_d].calls_in == 1
    assert rows[yesterday_d].calls_missed == 1


def test_roll_up_idempotent(tenant_db):
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    _add_call(tenant_db, today, direction="in", status="answered")
    tenant_db.commit()

    roll_up_recent(tenant_db, days=2)
    first = tenant_db.query(PhoneComStatsDaily).filter(
        PhoneComStatsDaily.stat_date == today.date(),
    ).one()
    assert first.calls_in == 1

    # Re-run — same data, same result, no duplicate row.
    roll_up_recent(tenant_db, days=2)
    second = tenant_db.query(PhoneComStatsDaily).filter(
        PhoneComStatsDaily.stat_date == today.date(),
    ).one()
    assert second.calls_in == 1
    assert tenant_db.query(PhoneComStatsDaily).count() == 2  # today + yesterday only


def test_roll_up_all_history_walks_distinct_dates(tenant_db):
    base = datetime(2026, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
    _add_call(tenant_db, base, direction="in")
    _add_call(tenant_db, base + timedelta(days=5), direction="in")
    _add_call(tenant_db, base + timedelta(days=20), direction="out")
    tenant_db.add(PhoneComVoicemail(
        phone_com_voicemail_id="vm-1",
        created_at=base + timedelta(days=10),
        raw_payload={},
    ))
    tenant_db.add(PhoneComMessage(
        phone_com_message_id="msg-1",
        thread_key="t1",
        direction="in",
        sent_at=base + timedelta(days=15),
        attachments=[],
        raw_payload={},
    ))
    tenant_db.commit()

    result = roll_up_all_history(tenant_db)
    assert result["days_rolled_up"] == 5  # 4 unique call dates + voicemail + message... wait
    rows = tenant_db.query(PhoneComStatsDaily).count()
    assert rows == 5  # 2026-01-01, -06, -11 (vm), -16 (msg), -21


def test_voicemails_new_counted_per_creation_date(tenant_db):
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    tenant_db.add(PhoneComVoicemail(
        phone_com_voicemail_id="vm-a",
        created_at=today,
        raw_payload={},
    ))
    tenant_db.add(PhoneComVoicemail(
        phone_com_voicemail_id="vm-b",
        created_at=today - timedelta(days=2),
        raw_payload={},
    ))
    tenant_db.commit()
    roll_up_recent(tenant_db, days=7)
    today_row = tenant_db.query(PhoneComStatsDaily).filter(
        PhoneComStatsDaily.stat_date == today.date(),
    ).one()
    assert today_row.voicemails_new == 1
