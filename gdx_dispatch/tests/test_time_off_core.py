"""core/time_off.py — the hours rules for paid time off.

Pinned here because a wrong answer in any of these pays somebody for a day
they were not owed, or fails to pay one they were:

* workday expansion honours the shop's bitmask and a person's override, and
  refuses an empty week rather than paying seven days;
* the synthetic stamps land on the shop-local day across a DST change;
* creation is idempotent per (person, shop day) — approving twice, or
  posting a holiday twice, never doubles a day;
* the holiday calendar rejects what the settings screen must not save.
"""
from __future__ import annotations

from collections.abc import Generator
from datetime import date, time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.pay_periods import shop_day_of
from gdx_dispatch.core.time_off import (
    create_time_off_entries,
    existing_time_off_days,
    normalize_holiday_calendar,
    person_schedule,
    posted_holiday_counts,
    time_off_stamps,
    user_names,
    workdays_between,
)
from gdx_dispatch.models.tenant_models import AppSettings, TimeclockEntry, User

TENANT = "tenant-to"
TZ = "America/Chicago"


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _settings(db: Session, **overrides) -> AppSettings:
    row = AppSettings(company_name="Shop", timezone=TZ, **overrides)
    db.add(row)
    db.commit()
    return row


# ── workdays ─────────────────────────────────────────────────────────────────


def test_workdays_between_skips_the_weekend_on_the_default_mask():
    # Fri 2026-10-02 .. Mon 2026-10-05
    days = workdays_between(date(2026, 10, 2), date(2026, 10, 5), 31)
    assert days == [date(2026, 10, 2), date(2026, 10, 5)]


def test_workdays_between_honours_a_saturday_mask():
    days = workdays_between(date(2026, 10, 2), date(2026, 10, 5), 31 | 32)
    assert days == [date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 5)]


def test_workdays_between_refuses_an_empty_mask_and_a_backwards_range():
    assert workdays_between(date(2026, 10, 2), date(2026, 10, 5), 0) == []
    assert workdays_between(date(2026, 10, 2), date(2026, 10, 5), None) == []
    assert workdays_between(date(2026, 10, 5), date(2026, 10, 2), 31) == []


def test_person_schedule_falls_back_to_the_shop_then_honours_the_override(db):
    settings = _settings(db, default_shift_start=time(7, 30), default_workdays=31)
    uid = uuid4()
    db.add(User(id=uid, company_id=TENANT, email="t@x", role="technician"))
    db.commit()
    sched = person_schedule(db, settings, str(uid))
    assert sched.shift_start == time(7, 30)
    assert sched.workdays == 31

    user = db.execute(select(User).where(User.id == uid)).scalars().one()
    user.shift_start = time(6, 0)
    user.workdays = 31 | 32
    db.commit()
    sched = person_schedule(db, settings, str(uid))
    assert sched.shift_start == time(6, 0)
    assert sched.workdays == 63

    # An id that is not a user (the timeclock's legacy text ids) → shop default.
    sched = person_schedule(db, settings, "tech-user-1")
    assert sched.shift_start == time(7, 30)


# ── stamps ───────────────────────────────────────────────────────────────────


def test_stamps_follow_the_shop_clock_across_the_dst_change():
    # US DST began 2026-03-08. Chicago 08:00 is 14:00Z the day before and
    # 13:00Z the day after; the shop-local day is the same either way.
    before_in, before_out = time_off_stamps(date(2026, 3, 6), time(8, 0), 480, TZ)
    after_in, after_out = time_off_stamps(date(2026, 3, 9), time(8, 0), 480, TZ)
    assert before_in == "2026-03-06T14:00:00+00:00"
    assert before_out == "2026-03-06T22:00:00+00:00"
    assert after_in == "2026-03-09T13:00:00+00:00"
    assert after_out == "2026-03-09T21:00:00+00:00"
    assert shop_day_of(before_in, TZ) == date(2026, 3, 6)
    assert shop_day_of(after_in, TZ) == date(2026, 3, 9)


def test_an_evening_shift_start_still_lands_on_its_own_shop_day():
    # 22:00 Chicago is 03:00Z the next day; a UTC bucket would misfile it.
    clock_in, _ = time_off_stamps(date(2026, 6, 1), time(22, 0), 240, TZ)
    assert clock_in.startswith("2026-06-02T03:00")
    assert shop_day_of(clock_in, TZ) == date(2026, 6, 1)


# ── creation ─────────────────────────────────────────────────────────────────


def test_create_writes_closed_entries_and_skips_days_already_taken(db):
    settings = _settings(db, default_shift_start=time(8, 0), default_workdays=31)
    days = [date(2026, 10, 2), date(2026, 10, 5)]
    first = create_time_off_entries(
        db, tenant_id=TENANT, technician_id="tech-a", entry_type="vacation",
        days=days, minutes=480, notes="beach", settings=settings,
    )
    db.commit()
    assert len(first.created) == 2 and first.skipped == []
    rows = db.execute(select(TimeclockEntry)).scalars().all()
    assert {r.entry_type for r in rows} == {"vacation"}
    assert all(r.clock_out_at and r.minutes == 480 and r.notes == "beach" for r in rows)
    assert sorted(shop_day_of(r.clock_in_at, TZ) for r in rows) == days

    # Second pass over an overlapping range: nothing doubled, the overlap named.
    again = create_time_off_entries(
        db, tenant_id=TENANT, technician_id="tech-a", entry_type="holiday",
        days=[date(2026, 10, 5), date(2026, 10, 6)], minutes=480, notes=None, settings=settings,
    )
    db.commit()
    assert [str(e.id) for e in again.created] and len(again.created) == 1
    assert again.skipped == [date(2026, 10, 5)]
    assert again.created[0].notes == "Holiday"
    assert db.execute(select(TimeclockEntry)).scalars().all().__len__() == 3


def test_create_refuses_a_type_that_is_not_time_off(db):
    with pytest.raises(ValueError):
        create_time_off_entries(
            db, tenant_id=TENANT, technician_id="tech-a", entry_type="clock",
            days=[date(2026, 10, 2)], minutes=480, notes=None, settings=None,
        )


def test_existing_days_ignores_worked_shifts_and_deleted_rows(db):
    day = date(2026, 10, 2)
    db.add(TimeclockEntry(
        id="w1", tenant_id=TENANT, technician_id="tech-a",
        clock_in_at="2026-10-02T13:00:00+00:00", clock_out_at="2026-10-02T21:00:00+00:00",
        minutes=480, entry_type="clock", created_at="x", updated_at="x",
    ))
    db.add(TimeclockEntry(
        id="v-deleted", tenant_id=TENANT, technician_id="tech-a",
        clock_in_at="2026-10-02T13:00:00+00:00", clock_out_at="2026-10-02T21:00:00+00:00",
        minutes=480, entry_type="vacation", created_at="x", updated_at="x",
        deleted_at="2026-10-01T00:00:00+00:00",
    ))
    db.commit()
    assert existing_time_off_days(db, "tech-a", [day], TZ) == {}


def test_posted_holiday_counts_people_not_rows(db):
    for tech in ("a", "b"):
        db.add(TimeclockEntry(
            id=f"h-{tech}", tenant_id=TENANT, technician_id=tech,
            clock_in_at="2026-12-25T14:00:00+00:00", clock_out_at="2026-12-25T22:00:00+00:00",
            minutes=480, entry_type="holiday", created_at="x", updated_at="x",
        ))
    db.commit()
    counts = posted_holiday_counts(db, [date(2026, 12, 25), date(2026, 12, 24)], TZ)
    assert counts == {date(2026, 12, 25): 2}


def test_user_names_resolve_uuid_ids_and_ignore_the_rest(db):
    uid = uuid4()
    db.add(User(id=uid, company_id=TENANT, full_name="Amber Rosa", role="technician"))
    db.commit()
    names = user_names(db, {str(uid), "tech-user-1", ""})
    assert names == {str(uid): "Amber Rosa"}


# ── holiday calendar ─────────────────────────────────────────────────────────


def test_calendar_is_canonicalised_sorted_and_deduplicated_by_error():
    out = normalize_holiday_calendar([
        {"date": "2026-12-25", "name": " Christmas Day ", "minutes": 480},
        {"date": date(2026, 7, 3), "name": "Independence Day (observed)"},
    ])
    assert out == [
        {"date": "2026-07-03", "name": "Independence Day (observed)", "minutes": 480},
        {"date": "2026-12-25", "name": "Christmas Day", "minutes": 480},
    ]
    assert normalize_holiday_calendar(None) == []
    assert normalize_holiday_calendar([]) == []


@pytest.mark.parametrize(
    "bad, fragment",
    [
        ([{"date": "not-a-date", "name": "X"}], "not a date"),
        ([{"date": "2026-12-25", "name": ""}], "needs a name"),
        ([{"date": "2026-12-25", "name": "X", "minutes": 5}], "between"),
        ([{"date": "2026-12-25", "name": "X", "minutes": 2000}], "between"),
        ([{"date": "2026-12-25", "name": "X", "minutes": "eight"}], "must be a number"),
        ([{"date": "2026-12-25", "name": "X"}, {"date": "2026-12-25", "name": "Y"}], "two holidays"),
        ("nope", "must be a list"),
        (["nope"], "needs a date"),
    ],
)
def test_calendar_rejects_what_settings_must_not_save(bad, fragment):
    with pytest.raises(ValueError) as exc:
        normalize_holiday_calendar(bad)
    assert fragment in str(exc.value)
