"""Phone.com daily stats roll-up — Sprint phone-com fix-it Wave D / S6.

Reads phone_com_calls + phone_com_messages + phone_com_voicemails for a
single tenant, groups them by `stat_date` (UTC date of started_at /
sent_at / created_at), and upserts one row per date into
phone_com_stats_daily.

Two entry points:
- :func:`roll_up_recent` — last N days only (default 7). Cheap; runs
  inside the per-tenant worker after every successful sync.
- :func:`roll_up_all_history` — full table sweep. One-shot, used by the
  prod backfill so the dashboard shows history before the next sync.

Heuristics for `calls_missed`:
- voicemail_received status implies the call rolled to voicemail (missed
  by a human at the time, even if VM was checked later).
- status containing 'missed' / 'busy' / 'no_answer' covers carrier-level
  no-pickup.
- Forwarded-then-answered counts as `calls_in`, not missed.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from gdx_dispatch.modules.phone_com.models import (
    PhoneComCall,
    PhoneComMessage,
    PhoneComStatsDaily,
    PhoneComVoicemail,
)

log = logging.getLogger("gdx_dispatch.modules.phone_com.stats")


def _is_missed(status: str | None) -> bool:
    if not status:
        return False
    s = status.lower()
    return (
        "voicemail_received" in s
        or "missed" in s
        or "no_answer" in s
        or "busy" in s
    )


def _aggregate_one_day(tenant_db: Session, day: date) -> dict[str, Any]:
    """Compute the stats row for a single UTC date."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    calls = (
        tenant_db.query(PhoneComCall)
        .filter(PhoneComCall.started_at >= start)
        .filter(PhoneComCall.started_at < end)
        .all()
    )
    calls_in = sum(1 for c in calls if c.direction == "in")
    calls_out = sum(1 for c in calls if c.direction == "out")
    calls_missed = sum(1 for c in calls if _is_missed(c.status))
    total_call_minutes = sum((c.duration_s or 0) for c in calls) // 60

    msgs = (
        tenant_db.query(PhoneComMessage)
        .filter(PhoneComMessage.sent_at >= start)
        .filter(PhoneComMessage.sent_at < end)
        .all()
    )
    sms_in = sum(1 for m in msgs if m.direction == "in")
    sms_out = sum(1 for m in msgs if m.direction == "out")

    voicemails_new = (
        tenant_db.query(func.count(PhoneComVoicemail.id))
        .filter(PhoneComVoicemail.created_at >= start)
        .filter(PhoneComVoicemail.created_at < end)
        .scalar()
    ) or 0

    return {
        "calls_in": calls_in,
        "calls_out": calls_out,
        "calls_missed": calls_missed,
        "sms_in": sms_in,
        "sms_out": sms_out,
        "voicemails_new": voicemails_new,
        "total_call_minutes": total_call_minutes,
    }


def _upsert_stats_daily(
    tenant_db: Session, day: date, agg: dict[str, Any]
) -> PhoneComStatsDaily:
    row = (
        tenant_db.query(PhoneComStatsDaily)
        .filter(PhoneComStatsDaily.stat_date == day)
        .first()
    )
    if row is None:
        row = PhoneComStatsDaily(stat_date=day, raw_payload={})
        tenant_db.add(row)
    row.calls_in = agg["calls_in"]
    row.calls_out = agg["calls_out"]
    row.calls_missed = agg["calls_missed"]
    row.sms_in = agg["sms_in"]
    row.sms_out = agg["sms_out"]
    row.voicemails_new = agg["voicemails_new"]
    row.total_call_minutes = agg["total_call_minutes"]
    return row


def roll_up_recent(tenant_db: Session, *, days: int = 7) -> dict[str, int]:
    """Roll up the last `days` days of stats. Default 7."""
    today = datetime.now(timezone.utc).date()
    written = 0
    for offset in range(days):
        d = today - timedelta(days=offset)
        agg = _aggregate_one_day(tenant_db, d)
        _upsert_stats_daily(tenant_db, d, agg)
        written += 1
    tenant_db.commit()
    log.info("phone_com.stats.roll_up_recent days=%d written=%d", days, written)
    return {"days_rolled_up": written}


def roll_up_all_history(tenant_db: Session) -> dict[str, int]:
    """Walk every distinct date that has a call / message / voicemail and
    write a stats_daily row for it. Idempotent. Used by the one-shot
    historical backfill on tenants that started Phone.com sync after data
    already accumulated.

    Implementation: stream timestamps and dedupe by `.date()` in Python.
    Avoids the date_trunc dialect split (SQLite has no date_trunc; tests
    use SQLite, prod uses Postgres)."""
    dates: set[date] = set()
    for (ts,) in tenant_db.query(PhoneComCall.started_at).filter(
        PhoneComCall.started_at.isnot(None),
    ).all():
        if ts is not None:
            dates.add(ts.date())
    for (ts,) in tenant_db.query(PhoneComMessage.sent_at).filter(
        PhoneComMessage.sent_at.isnot(None),
    ).all():
        if ts is not None:
            dates.add(ts.date())
    for (ts,) in tenant_db.query(PhoneComVoicemail.created_at).filter(
        PhoneComVoicemail.created_at.isnot(None),
    ).all():
        if ts is not None:
            dates.add(ts.date())

    for d in sorted(dates):
        agg = _aggregate_one_day(tenant_db, d)
        _upsert_stats_daily(tenant_db, d, agg)
    tenant_db.commit()
    log.info("phone_com.stats.roll_up_all_history days_written=%d", len(dates))
    return {"days_rolled_up": len(dates)}
