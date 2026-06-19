from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event
from gdx_dispatch.modules.timeclock.models import TimeClock


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _audit(db: Session, event_type: str, actor_id: str, entity_id: str, payload: dict) -> None:
    asyncio.run(log_audit_event(db, event_type, actor_id, "timeclock", entity_id, payload))


def clock_in(technician_id: str, job_id, db: Session, tenant_timezone: str = "America/New_York", company_id: str = "") -> TimeClock:
    open_row = db.execute(select(TimeClock).where(TimeClock.technician_id == technician_id, TimeClock.clock_out_at.is_(None))).scalar_one_or_none()
    if open_row:
        raise ValueError("Already clocked in")
    if not company_id:
        raise ValueError("company_id is required for tenant isolation")
    row = TimeClock(technician_id=technician_id, job_id=job_id, clock_in_at=_utcnow(), company_id=company_id)
    db.add(row); db.flush(); _audit(db, "clock_in", technician_id, str(row.id), {"job_id": str(job_id) if job_id else None, "tenant_timezone": tenant_timezone}); db.commit(); db.refresh(row)  # noqa: E701,E702
    return row


def clock_out(timeclock_id, db: Session) -> TimeClock:
    row = db.execute(select(TimeClock).where(TimeClock.id == timeclock_id)).scalar_one_or_none()
    if not row:
        raise ValueError("Time clock not found")
    if row.clock_out_at is not None:
        raise ValueError("Already clocked out")
    row.clock_out_at = _utcnow()
    # SQLite returns naive datetimes — normalise before subtraction
    clock_in = row.clock_in_at
    if clock_in.tzinfo is None:
        clock_in = clock_in.replace(tzinfo=timezone.utc)
    row.labor_minutes = int((row.clock_out_at - clock_in).total_seconds() / 60)
    _audit(db, "clock_out", row.technician_id, str(row.id), {"labor_minutes": row.labor_minutes}); db.commit(); db.refresh(row)  # noqa: E701,E702
    return row


def daily_labor_report(report_date: date, tenant_timezone: str, db: Session) -> list[dict]:
    tz = ZoneInfo(tenant_timezone)
    start_local = datetime.combine(report_date, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    start_utc, end_utc = start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
    rows = db.execute(select(TimeClock).where(TimeClock.clock_in_at >= start_utc, TimeClock.clock_in_at < end_utc).order_by(TimeClock.clock_in_at.asc())).scalars().all()
    return [{"technician_id": r.technician_id, "job_id": r.job_id, "labor_minutes": r.labor_minutes, "clock_in_at": r.clock_in_at, "clock_out_at": r.clock_out_at} for r in rows]
