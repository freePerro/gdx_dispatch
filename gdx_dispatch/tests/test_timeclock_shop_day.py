"""Timeclock "today" is the shop's day, not the UTC prefix (GDXA-421).

The desktop time clock's /status figure and /submit-day both keyed the day on
the UTC date of the stamp (`date(clock_in_at)`, `LIKE 'YYYY-MM-DD%'`) and on
the container's clock. For a Minnesota shop that rolls "today" over at 7pm:
an evening entry fell out of today's total, and an evening submit attested
tomorrow's UTC day with 0 entries — which is also the key the office badge
(/submitted-days) shows.

The clock here is 23:30 Central on 2026-10-09, i.e. 04:30 UTC on 2026-10-10.

The handlers are called directly, as in test_timeclock_status_breaks: under
freezegun a mounted FastAPI route resolves its `date` annotations to
freezegun's FakeDate and cannot build its schema.
"""
from __future__ import annotations

from datetime import date
from uuid import uuid4

import freezegun
import pytest
from fastapi import HTTPException
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models.tenant_models import AppSettings, TimeclockEntry
from gdx_dispatch.routers import timeclock as tc

freezegun.configure(extend_ignore_list=["_pytest"])

TENANT = "tenant-a"
TECH = {"user_id": "user-1", "sub": "user-1", "role": "technician", "tenant_id": TENANT}
OFFICE = {"user_id": "office-1", "sub": "office-1", "role": "dispatcher", "tenant_id": TENANT}
SHOP_DAY = "2026-10-09"
NOW = "2026-10-10 04:30:00"  # 23:30 CDT on SHOP_DAY


def _request() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    return req


def _entry(db, clock_in: str, clock_out: str | None, minutes: int | None) -> None:
    db.add(TimeclockEntry(
        id=str(uuid4()), tenant_id=TENANT, technician_id="user-1",
        clock_in_at=clock_in, clock_out_at=clock_out, minutes=minutes,
        entry_type="clock", created_at=clock_in, updated_at=clock_in,
    ))


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'shop_day.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session.add(AppSettings(timezone="America/Chicago"))
    session.commit()
    with freeze_time(NOW):
        yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def seeded(db):
    # 10:00-11:00 CDT on the shop day: UTC-dated the same day.
    _entry(db, "2026-10-09T15:00:00+00:00", "2026-10-09T16:00:00+00:00", 60)
    # 22:00-23:15 CDT on the shop day: UTC-dated TOMORROW.
    _entry(db, "2026-10-10T03:00:00+00:00", "2026-10-10T04:15:00+00:00", 75)
    # 22:00-23:00 CDT the day BEFORE: UTC-dated the shop day.
    _entry(db, "2026-10-09T03:00:00+00:00", "2026-10-09T04:00:00+00:00", 60)
    db.commit()
    return db


def _status(db):
    return tc.get_timeclock_status(
        request=_request(), technician_id=None, current_user=TECH, db=db
    )


def _submit(db, day: str | None = None):
    return tc.submit_day(
        payload=tc.SubmitDayPayload(date=day), request=_request(), current_user=TECH, db=db
    )


def test_status_today_counts_the_evening_entry_and_not_last_night(seeded):
    """135 min = the 10:00 and 22:00 shifts of the shop day; the previous
    evening's shift (UTC-dated today) is not today's."""
    assert _status(seeded).today_hours == pytest.approx(2.25)


def test_open_overnight_shift_counts_only_since_shop_midnight(db):
    """An open shift from 20:00 CDT yesterday contributes the 23:30 since the
    shop's midnight, not the 4:30 since UTC midnight."""
    _entry(db, "2026-10-09T01:00:00+00:00", None, None)
    db.commit()
    st = _status(db)
    assert st.clocked_in is True
    assert st.today_hours == pytest.approx(23.5, abs=0.05)


def test_submit_day_defaults_to_the_shop_day_and_counts_its_entries(seeded):
    res = _submit(seeded)
    assert (res.date, res.entries, res.total_minutes) == (SHOP_DAY, 2, 135)
    rows = seeded.execute(
        select(AuditLog).where(AuditLog.action == "timeclock_day_submitted")
    ).scalars().all()
    assert [r.entity_id for r in rows] == [SHOP_DAY]


def test_submit_day_with_the_shop_date_matches_by_shop_day(seeded):
    """The desktop now sends shopToday(); the 22:00 entry, UTC-dated the next
    day, is counted, and last night's UTC-dated-today entry is not."""
    res = _submit(seeded, SHOP_DAY)
    assert (res.entries, res.total_minutes) == (2, 135)


def test_the_office_badge_shows_the_shop_day(seeded):
    _submit(seeded)
    rows = tc.list_submitted_days(
        request=_request(),
        date_start=date(2026, 10, 9),
        date_end=date(2026, 10, 9),
        current_user=OFFICE,
        db=seeded,
    )
    assert [(r.technician_id, r.date) for r in rows] == [("user-1", SHOP_DAY)]


def test_submit_day_rejects_a_malformed_date(db):
    with pytest.raises(HTTPException) as exc:
        _submit(db, "tomorrow")
    assert exc.value.status_code == 422


def _break(db, started_at: str, minutes: int) -> None:
    db.add(tc.TimeclockBreak(
        id=str(uuid4()), tenant_id=TENANT, user_id="user-1", type="lunch",
        started_at=started_at, ended_at=started_at, duration_minutes=minutes,
        created_at=started_at,
    ))


def test_breaks_are_netted_on_the_shop_day_they_started(seeded):
    """A 15 min break at 22:30 CDT (UTC-dated tomorrow) comes off today; a
    5 min break at 22:30 CDT last night (UTC-dated today) does not. Under a
    UTC `date(started_at)` match the two swap: today nets 5, not 15."""
    _break(seeded, "2026-10-10T03:30:00+00:00", 15)
    _break(seeded, "2026-10-09T03:30:00+00:00", 5)
    seeded.commit()
    assert _status(seeded).today_hours == pytest.approx(2.0)  # 135 - 15 min
