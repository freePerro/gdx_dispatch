"""Hours for a pay period — the numbers that get emailed.

These are the rules that decide what a person is paid for, so each test
below is named for the way it goes wrong rather than the feature it covers:

* Totalling `minutes` pays out every lunch. Break time lives in its own
  table and is never subtracted by the clock.
* Bucketing a shift by its UTC date moves an evening shift a day, and on a
  period boundary moves it into the wrong fortnight entirely.
* Treating an unknown duration as zero WITHOUT flagging it produces a file
  that looks complete and quietly under-pays somebody.
* Flagging every open shift makes the count wallpaper — at 10am with techs
  on the clock it would read "4 to fix" while Dispatch reads 0.

The last one is the reason `flag_for` carries a threshold rather than a
null check, and it is asserted from both directions.
"""
from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.pay_periods import PayPeriod
from gdx_dispatch.core.timesheet_hours import (
    FLAG_IMPLAUSIBLE,
    FLAG_OPEN,
    FLAG_UNKNOWN,
    IMPLAUSIBLE_SHIFT_MINUTES,
    MAX_SHIFT_HOURS,
    build_timesheet,
    flag_for,
)
from gdx_dispatch.models.tenant_models import TimeclockBreak, TimeclockEntry

TENANT = "tenant-test"
TZ = "America/Chicago"
MICHAEL = "user-michael"
AMBER = "user-amber"

# Doug's real fortnight.
PERIOD = PayPeriod(date(2026, 8, 10), date(2026, 8, 23))
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


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


def _shift(
    db: Session,
    *,
    entry_id: str,
    tech: str = MICHAEL,
    clock_in: str,
    clock_out: str | None = None,
    minutes: int | None = 480,
    entry_type: str = "clock",
) -> None:
    db.add(
        TimeclockEntry(
            id=entry_id,
            tenant_id=TENANT,
            technician_id=tech,
            clock_in_at=clock_in,
            clock_out_at=clock_out,
            minutes=minutes,
            notes=None,
            entry_type=entry_type,
            created_at=clock_in,
            updated_at=clock_in,
        )
    )
    db.commit()


def _break(db: Session, *, break_id: str, user: str, started_at: str, minutes: int) -> None:
    db.add(
        TimeclockBreak(
            id=break_id,
            tenant_id=TENANT,
            user_id=user,
            type="lunch",
            started_at=started_at,
            ended_at=started_at,
            duration_minutes=minutes,
            created_at=started_at,
        )
    )
    db.commit()


def _sheet(db: Session, **kw):
    return build_timesheet(
        db, tenant_id=TENANT, period=PERIOD, tz_name=TZ, now=NOW, **kw
    )


# ---------------------------------------------------------------------------
# Break netting
# ---------------------------------------------------------------------------

def test_lunch_is_not_paid_for(db: Session):
    """`minutes` is gross elapsed. Totalling it pays every lunch."""
    _shift(db, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    _break(db, break_id="b1", user=MICHAEL, started_at="2026-08-17T17:00:00+00:00", minutes=30)

    sheet = _sheet(db)
    assert sheet.timecards[0].worked_hours == 8.5
    assert sheet.timecards[0].break_minutes == 30


def test_an_unfinished_break_subtracts_nothing(db: Session):
    """An open break has no defensible length; inventing one fabricates a
    deduction from somebody's pay."""
    _shift(db, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    db.add(TimeclockBreak(
        id="b-open", tenant_id=TENANT, user_id=MICHAEL, type="lunch",
        started_at="2026-08-17T17:00:00+00:00", ended_at=None,
        duration_minutes=None, created_at="2026-08-17T17:00:00+00:00",
    ))
    db.commit()

    assert _sheet(db).timecards[0].worked_hours == 9.0


def test_a_break_from_another_day_is_not_netted_off_this_shift(db: Session):
    _shift(db, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    _break(db, break_id="b1", user=MICHAEL, started_at="2026-08-19T17:00:00+00:00", minutes=45)

    assert _sheet(db).timecards[0].worked_hours == 9.0


def test_worked_minutes_never_goes_negative(db: Session):
    """A break longer than the shift is bad data, not a negative paycheck."""
    _shift(db, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T14:00:00+00:00", minutes=60)
    _break(db, break_id="b1", user=MICHAEL, started_at="2026-08-17T13:30:00+00:00", minutes=120)

    assert _sheet(db).timecards[0].worked_minutes == 0


# ---------------------------------------------------------------------------
# Shop-local days and the period boundary
# ---------------------------------------------------------------------------

def test_an_evening_shift_stays_in_the_period_it_was_worked(db: Session):
    """23:30 Central on 8/23 is stored as 04:30 UTC on 8/24 — the first day
    of the NEXT fortnight. Filtering on the stored text drops it out of the
    period it belongs to, and out of the check that pays it."""
    _shift(db, entry_id="e-late", clock_in="2026-08-24T04:30:00+00:00",
           clock_out="2026-08-24T07:30:00+00:00", minutes=180)

    sheet = _sheet(db)
    assert sheet.shift_count == 1, "the late shift must be inside this period"
    assert sheet.timecards[0].shifts[0].day == date(2026, 8, 23)


def test_a_shift_after_the_period_really_is_excluded(db: Session):
    """The counterfactual for the test above: widening the query must not
    turn into including the next fortnight."""
    _shift(db, entry_id="e-next", clock_in="2026-08-24T14:00:00+00:00",
           clock_out="2026-08-24T22:00:00+00:00", minutes=480)

    assert _sheet(db).shift_count == 0


def test_a_shift_before_the_period_is_excluded(db: Session):
    _shift(db, entry_id="e-prev", clock_in="2026-08-09T14:00:00+00:00",
           clock_out="2026-08-09T22:00:00+00:00", minutes=480)

    assert _sheet(db).shift_count == 0


def test_the_first_and_last_days_are_inside(db: Session):
    _shift(db, entry_id="e-first", clock_in="2026-08-10T14:00:00+00:00",
           clock_out="2026-08-10T22:00:00+00:00", minutes=480)
    _shift(db, entry_id="e-last", clock_in="2026-08-23T14:00:00+00:00",
           clock_out="2026-08-23T22:00:00+00:00", minutes=480)

    assert _sheet(db).shift_count == 2


def test_a_soft_deleted_shift_is_not_paid(db: Session):
    _shift(db, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    entry = db.get(TimeclockEntry, "e1")
    entry.deleted_at = "2026-08-18T00:00:00+00:00"
    db.commit()

    assert _sheet(db).shift_count == 0


# ---------------------------------------------------------------------------
# Flags — what stops the send
# ---------------------------------------------------------------------------

def test_a_shift_still_open_after_a_possible_day_is_flagged(db: Session):
    _shift(db, entry_id="e-open", clock_in="2026-08-20T13:00:00+00:00",
           clock_out=None, minutes=None)

    sheet = _sheet(db)
    assert [s.flag for s in sheet.timecards[0].shifts] == [FLAG_OPEN]
    assert not sheet.is_clean


def test_someone_merely_on_the_clock_right_now_is_not_flagged():
    """The wallpaper guard. Flagging every null clock_out would read
    "4 to fix" at 10am with four techs working."""
    started = (NOW - timedelta(hours=2)).isoformat()
    assert flag_for(clock_out=None, minutes=None, clock_in=started, now=NOW) is None


def test_an_open_shift_past_the_ceiling_is_flagged():
    started = (NOW - timedelta(hours=MAX_SHIFT_HOURS + 1)).isoformat()
    assert flag_for(clock_out=None, minutes=None, clock_in=started, now=NOW) == FLAG_OPEN


def test_a_closed_shift_with_no_duration_is_flagged(db: Session):
    """Counts as zero hours, which is why it MUST be flagged: a silent zero
    is indistinguishable from a day off in a spreadsheet."""
    _shift(db, entry_id="e-unknown", clock_in="2026-08-18T13:00:00+00:00",
           clock_out="2026-08-18T22:00:00+00:00", minutes=None)

    sheet = _sheet(db)
    assert sheet.timecards[0].shifts[0].flag == FLAG_UNKNOWN
    assert sheet.timecards[0].worked_minutes == 0
    assert not sheet.is_clean


def test_an_impossible_shift_is_flagged(db: Session):
    _shift(db, entry_id="e-long", clock_in="2026-08-18T13:00:00+00:00",
           clock_out="2026-08-20T13:00:00+00:00", minutes=IMPLAUSIBLE_SHIFT_MINUTES + 1)

    assert _sheet(db).timecards[0].shifts[0].flag == FLAG_IMPLAUSIBLE


def test_a_long_but_possible_day_is_not_flagged(db: Session):
    """A genuine 15-hour day must pass, or the only way to clear the gate
    would be to edit true data into false data."""
    _shift(db, entry_id="e-15h", clock_in="2026-08-18T11:00:00+00:00",
           clock_out="2026-08-19T02:00:00+00:00", minutes=15 * 60)

    sheet = _sheet(db)
    assert sheet.timecards[0].shifts[0].flag is None
    assert sheet.is_clean


def test_a_zero_minute_double_tap_is_not_flagged(db: Session):
    """21 of 39 rows on prod. They pay nothing and cost nothing; surfacing
    them on day one is the flood the exceptions card refuses to become."""
    _shift(db, entry_id="e-tap", clock_in="2026-08-18T13:00:00+00:00",
           clock_out="2026-08-18T13:00:10+00:00", minutes=0)

    sheet = _sheet(db)
    assert sheet.timecards[0].shifts[0].flag is None
    assert sheet.is_clean


def test_a_clean_period_reports_clean(db: Session):
    _shift(db, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)

    assert _sheet(db).is_clean


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def test_each_person_gets_their_own_card(db: Session):
    _shift(db, entry_id="e1", tech=MICHAEL, clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    _shift(db, entry_id="e2", tech=AMBER, clock_in="2026-08-18T14:00:00+00:00",
           clock_out="2026-08-18T18:00:00+00:00", minutes=240)

    sheet = _sheet(db, names={MICHAEL: "Michael Tallman", AMBER: "Amber Joy Rosa"})
    assert [c.name for c in sheet.timecards] == ["Amber Joy Rosa", "Michael Tallman"]
    assert sheet.people == 2
    assert sheet.worked_hours == 13.0


def test_someone_whose_user_row_is_gone_still_appears(db: Session):
    """Their hours are still on the books. Dropping the row hides real pay."""
    _shift(db, entry_id="e1", tech="ghost-1234-5678", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)

    sheet = _sheet(db, names={})
    assert sheet.people == 1
    assert "ghost-12" in sheet.timecards[0].name
    assert sheet.timecards[0].worked_hours == 9.0


def test_a_single_person_can_be_isolated(db: Session):
    _shift(db, entry_id="e1", tech=MICHAEL, clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)
    _shift(db, entry_id="e2", tech=AMBER, clock_in="2026-08-18T14:00:00+00:00",
           clock_out="2026-08-18T18:00:00+00:00", minutes=240)

    sheet = _sheet(db, tech_id=AMBER)
    assert sheet.people == 1
    assert sheet.timecards[0].tech_id == AMBER


def test_days_are_grouped_within_a_card(db: Session):
    _shift(db, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T17:00:00+00:00", minutes=240)
    _shift(db, entry_id="e2", clock_in="2026-08-17T18:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=240)

    by_day = _sheet(db).timecards[0].by_day()
    assert list(by_day) == [date(2026, 8, 17)]
    assert len(by_day[date(2026, 8, 17)]) == 2


def test_an_empty_period_is_empty_not_an_error(db: Session):
    sheet = _sheet(db)
    assert sheet.timecards == []
    assert sheet.worked_hours == 0
    assert sheet.is_clean
