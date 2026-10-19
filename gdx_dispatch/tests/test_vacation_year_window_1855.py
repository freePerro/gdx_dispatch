"""Every vacation balance reader follows the shop's vacation-year policy (GDXA-1855).

GDXA-1827 stored `app_settings.vacation_year_policy` (`calendar_year`, the
default, or `hire_anniversary`, falling back to the calendar year for anyone
with no hire date — Doug, 2026-10-19) but no balance read it: the endpoint,
the Timesheets card, the pay-period CSV and the PDF all counted the calendar
year. They now take a per-person window from core/time_off.vacation_windows.

Pinned, each named for the failure it prevents:

* core's `vacation_year_start` answers exactly as the settings router's copy
  it replaces, and core imports no router;
* the window's last day is the day before the next anniversary, a 29
  February hire included;
* under the anniversary policy allowance, used and scheduled follow each
  person's own year; no hire date and a hire date still ahead read the
  calendar year;
* a pay period that crosses someone's anniversary is balanced against the
  year containing its last day, and the PDF says the days before it are not
  counted; the CSV's `vacation_year` is the year the window starts in;
* the read endpoint lists, and balances, people by their own year;
* calendar-year mode writes the same CSV bytes and the same PDF HTML as
  before this change (captured from c2f5c66c).

Real rows through the real code on ORM-built SQLite; nothing is mocked
except the one failed read.
"""
from __future__ import annotations

import hashlib
import inspect
import itertools
from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import OperationalError

from gdx_dispatch.core import time_off as time_off_rules
from gdx_dispatch.core.pay_periods import PayPeriod, shop_today
from gdx_dispatch.core.time_off import vacation_balances, vacation_window, vacation_year_start
from gdx_dispatch.core.timesheet_export import build_csv, pdf_context
from gdx_dispatch.core.timesheet_hours import build_timesheet
from gdx_dispatch.models.tenant_models import User
from gdx_dispatch.tests import test_time_off_balance as base
from gdx_dispatch.tests.test_silent_success_sweep import TENANT, _teardown

TZ, TECH, OTHER, OFFICE, PERIOD = base.TZ, base.TECH, base.OTHER, base.OFFICE, base.PERIOD
NAMES = {TECH: "Amber Rosa", OTHER: "Michael T"}
ANNIVERSARY = "hire_anniversary"


@pytest.fixture()
def tech():
    c = base._client()
    yield c
    _teardown(c)


def _policy(db, policy: str) -> None:
    base._settings(db).vacation_year_policy = policy
    db.commit()


def _hire(db, tid: str, hired: date | None) -> None:
    db.get(User, UUID(tid)).hire_date = hired
    db.commit()


def _sheet(db, period=PERIOD):
    return build_timesheet(db, tenant_id=TENANT, period=period, tz_name=TZ, names=NAMES,
                           with_vacation=True, settings=base._settings(db))


def _html(sheet) -> str:
    from gdx_dispatch.core.pdf_generator import _JINJA_ENV

    return _JINJA_ENV.get_template("timesheet_pdf.html").render(
        **pdf_context(sheet, branding={"company_name": "Shop"}, pay_date="2026-08-28", prepared_at="now"))


def _total(sheet, name: str) -> dict[str, str]:
    return next(r for r in base._rows(build_csv(sheet)) if r["employee"] == name and r["date"] == "TOTAL")


# ── the rule itself ──────────────────────────────────────────────────────────


def test_core_answers_as_the_settings_router_did_and_imports_no_router() -> None:
    from gdx_dispatch.routers.settings import vacation_year_start as router_copy

    hires = [None, date(2019, 4, 2), date(2019, 11, 30), date(2020, 2, 29), date(2019, 1, 1),
             date(2019, 12, 31), date(2026, 12, 1)]
    days = [date(2026, 1, 1), date(2026, 2, 28), date(2026, 3, 1), date(2026, 10, 19),
            date(2026, 12, 31), date(2028, 2, 28), date(2028, 2, 29), date(2028, 3, 1)]
    for policy, hired, on in itertools.product(("calendar_year", ANNIVERSARY, None, "fiscal_year"), hires, days):
        assert vacation_year_start(policy, hired, on) == router_copy(policy, hired, on), (policy, hired, on)
    assert "gdx_dispatch.routers" not in inspect.getsource(time_off_rules)


@pytest.mark.parametrize(
    ("policy", "hire", "on", "window"),
    [
        ("calendar_year", date(2019, 6, 15), date(2026, 8, 23), (date(2026, 1, 1), date(2026, 12, 31))),
        (ANNIVERSARY, date(2019, 6, 15), date(2026, 8, 23), (date(2026, 6, 15), date(2027, 6, 14))),
        (ANNIVERSARY, date(2019, 6, 15), date(2026, 6, 14), (date(2025, 6, 15), date(2026, 6, 14))),
        (ANNIVERSARY, date(2019, 1, 1), date(2026, 8, 23), (date(2026, 1, 1), date(2026, 12, 31))),
        # 29 February: Feb 28 in a common year, Feb 29 in a leap year; the
        # year before a leap anniversary runs to Feb 28 inclusive.
        (ANNIVERSARY, date(2020, 2, 29), date(2026, 3, 1), (date(2026, 2, 28), date(2027, 2, 27))),
        (ANNIVERSARY, date(2020, 2, 29), date(2027, 6, 1), (date(2027, 2, 28), date(2028, 2, 28))),
        (ANNIVERSARY, date(2020, 2, 29), date(2028, 3, 1), (date(2028, 2, 29), date(2029, 2, 27))),
        # No hire date, and one still ahead, read the calendar year.
        (ANNIVERSARY, None, date(2026, 8, 23), (date(2026, 1, 1), date(2026, 12, 31))),
        (ANNIVERSARY, date(2026, 11, 1), date(2026, 8, 23), (date(2026, 1, 1), date(2026, 12, 31))),
        # Hired today: the first vacation year starts today.
        (ANNIVERSARY, date(2026, 8, 23), date(2026, 8, 23), (date(2026, 8, 23), date(2027, 8, 22))),
    ],
)
def test_the_window_runs_to_the_day_before_the_next_year(policy, hire, on, window) -> None:
    assert vacation_window(policy, hire, on) == window
    assert (window[1] - window[0]).days in (364, 365)


# ── vacation_balances ────────────────────────────────────────────────────────


def test_under_the_anniversary_policy_each_person_counts_their_own_year(tech):
    with tech.SessionLocal() as db:
        base._seed(db, tech_hours="40")
        _hire(db, TECH, date(2019, 6, 15))                          # OTHER has no hire date
        base._entry(db, clock_in="2026-03-02T14:00:00+00:00")        # TECH: before Jun 15
        base._entry(db, clock_in="2026-06-20T13:00:00+00:00")        # TECH: in the year
        base._entry(db, clock_in="2027-01-11T14:00:00+00:00")        # TECH: in the year, ahead
        base._entry(db, tech=OTHER, clock_in="2026-03-02T14:00:00+00:00")
        base._entry(db, tech=OTHER, clock_in="2027-01-11T14:00:00+00:00")  # next calendar year
        db.commit()
        on = date(2026, 8, 23)

        cal = vacation_balances(db, base._settings(db), {TECH, OTHER}, on=on, as_of=on)
        assert (cal[TECH].used_hours, cal[TECH].scheduled_hours, cal[TECH].year) == (16.0, 0.0, 2026)
        assert cal[TECH].window == (date(2026, 1, 1), date(2026, 12, 31))

        _policy(db, ANNIVERSARY)
        out = vacation_balances(db, base._settings(db), {TECH, OTHER}, on=on, as_of=on)
        amber = out[TECH].as_dict()
        assert (amber["used_hours"], amber["taken_hours"], amber["scheduled_hours"]) == (16.0, 8.0, 8.0)
        assert (amber["window_start"], amber["window_end"], amber["year"]) == ("2026-06-15", "2027-06-14", 2026)
        assert (amber["allowance_hours"], amber["remaining_hours"]) == (40.0, 24.0)
        mike = out[OTHER].as_dict()
        assert (mike["window_start"], mike["window_end"]) == ("2026-01-01", "2026-12-31")
        assert (mike["used_hours"], mike["scheduled_hours"]) == (8.0, 0.0), "no hire date: the calendar year"


def test_a_29_february_hire_counts_from_28_february_in_a_common_year(tech):
    with tech.SessionLocal() as db:
        base._seed(db)
        _policy(db, ANNIVERSARY)
        _hire(db, TECH, date(2020, 2, 29))
        base._entry(db, clock_in="2026-02-27T14:00:00+00:00")        # the year before
        base._entry(db, clock_in="2026-02-28T14:00:00+00:00", minutes=240)
        db.commit()
        bal = vacation_balances(db, base._settings(db), {TECH}, on=date(2026, 3, 5), as_of=date(2026, 3, 5))[TECH]
        assert bal.window == (date(2026, 2, 28), date(2027, 2, 27)) and bal.used_minutes == 240
        before = vacation_balances(db, base._settings(db), {TECH}, on=date(2026, 2, 27), as_of=date(2026, 2, 27))[TECH]
        assert before.window == (date(2025, 2, 28), date(2026, 2, 27)) and before.used_minutes == 480


def test_a_hire_date_still_ahead_reads_the_calendar_year(tech):
    with tech.SessionLocal() as db:
        base._seed(db)
        _policy(db, ANNIVERSARY)
        _hire(db, TECH, date(2026, 11, 1))
        base._entry(db, clock_in="2026-03-02T14:00:00+00:00")
        db.commit()
        bal = vacation_balances(db, base._settings(db), {TECH}, on=date(2026, 8, 23), as_of=date(2026, 8, 23))[TECH]
        assert bal.window == (date(2026, 1, 1), date(2026, 12, 31)) and bal.used_hours == 8.0


# ── the pay-period files ─────────────────────────────────────────────────────


def _crossing_db(tech):
    """TECH's anniversary (Aug 15) falls inside PERIOD (Aug 10 – Aug 23)."""
    db = tech.SessionLocal()
    base._seed(db, tech_hours="40")
    _hire(db, TECH, date(2019, 8, 15))
    base._entry(db, clock_in="2026-03-02T14:00:00+00:00")    # last vacation year
    base._entry(db, clock_in="2026-08-12T13:00:00+00:00")    # in the period, last year
    base._entry(db, clock_in="2026-08-18T13:00:00+00:00")    # in the period, this year
    base._entry(db, clock_in="2026-09-14T13:00:00+00:00")    # after the period
    base._entry(db, tech=OTHER, clock_in="2026-08-17T13:00:00+00:00", entry_type="clock")
    db.commit()
    _policy(db, ANNIVERSARY)
    return db


def test_a_period_crossing_an_anniversary_balances_the_year_of_its_last_day(tech):
    db = _crossing_db(tech)
    try:
        sheet = _sheet(db)
        amber = _total(sheet, "Amber Rosa")
        assert amber["time_off_hours"] == "16.00", "the period's own hours are not cut"
        assert (amber["vacation_year"], amber["vacation_allowance_hours"], amber["vacation_used_hours"],
                amber["vacation_remaining_hours"], amber["vacation_booked_after_period_hours"]) == (
            "2026", "40.00", "8.00", "32.00", "8.00")
        mike = _total(sheet, "Michael T")
        assert mike["vacation_year"] == "2026" and mike["vacation_used_hours"] == "0.00"

        ctx = pdf_context(sheet)
        card = next(c for c in ctx["cards"] if c["name"] == "Amber Rosa")
        assert (card["vacation_label"], card["vacation_cut"]) == ("year from Aug 15, 2026", "Aug 15, 2026")
        assert ctx["vacation_prior_year"] is None and ctx["vacation_policy"] == ANNIVERSARY
        mcard = next(c for c in ctx["cards"] if c["name"] == "Michael T")
        assert (mcard["vacation_label"], mcard["vacation_cut"]) == ("year from Jan 1, 2026", None)

        text = base._pdf_text(sheet)
        block = base._person_block(text, "Amber Rosa")
        assert "Vacation year from Aug 15, 2026 through Aug 23, 2026: 40.00 allowance · 8.00 used · 32.00 remaining" in block
        assert "Vacation taken before Aug 15, 2026 is not counted in this balance." in block
        assert "is not counted" not in base._person_block(text, "Michael T")
        assert "the calendar year's allowance" not in text
        assert "from their hire anniversary; the calendar year without a hire date" in text
    finally:
        db.close()


def test_the_csv_vacation_year_is_the_year_the_window_starts_in(tech):
    db = tech.SessionLocal()
    try:
        base._seed(db, tech_hours="40")
        _hire(db, TECH, date(2019, 11, 30))
        base._entry(db, clock_in="2025-12-05T14:00:00+00:00")     # in the year from Nov 30, 2025
        base._entry(db, clock_in="2026-08-18T13:00:00+00:00")
        db.commit()
        _policy(db, ANNIVERSARY)
        amber = _total(_sheet(db), "Amber Rosa")
        assert (amber["vacation_year"], amber["vacation_used_hours"]) == ("2025", "16.00")
        _policy(db, "calendar_year")
        amber = _total(_sheet(db), "Amber Rosa")
        assert (amber["vacation_year"], amber["vacation_used_hours"]) == ("2026", "8.00")
    finally:
        db.close()


def test_a_failed_read_under_the_anniversary_policy_names_no_year(tech, monkeypatch):
    db = _crossing_db(tech)
    try:
        def boom(*_a, **_k):
            raise OperationalError("SELECT", {}, Exception("db gone"))

        monkeypatch.setattr(time_off_rules, "vacation_balances", boom)
        sheet = _sheet(db)
        amber = _total(sheet, "Amber Rosa")
        assert amber["vacation_year"] == "unavailable" and amber["vacation_used_hours"] == "unavailable"
        assert amber["time_off_hours"] == "16.00"
        block = base._person_block(" ".join(_html(sheet).split()), "Amber Rosa")
        assert "<strong>Vacation year</strong> &middot; balance unavailable" in block
        assert "Vacation 2026" not in block
    finally:
        db.close()


# ── the read endpoint ────────────────────────────────────────────────────────


def _anniversary_days_ago(today: date, days: int, years: int = 3) -> date:
    day = today - timedelta(days=days)
    if (day.month, day.day) == (2, 29):
        day -= timedelta(days=1)
    return day.replace(year=day.year - years)


def test_the_endpoint_reads_each_persons_own_year_and_lists_by_it(tech):
    today = shop_today(TZ)
    gone = str(uuid4())
    hired = _anniversary_days_ago(today, 5)
    with tech.SessionLocal() as db:
        base._seed(db, tech_hours="40")
        _hire(db, TECH, hired)
        # Departed, anniversary 5 days ago, vacation 20 days ago: outside the
        # year now in force, so not listed under the anniversary policy.
        db.add(User(id=UUID(gone), company_id=TENANT, full_name="Former Tech", role="technician",
                    deleted_at=today, hire_date=hired))
        base._entry(db, tech=gone, clock_in=f"{today - timedelta(days=20)}T18:00:00+00:00")
        base._entry(db, clock_in=f"{today - timedelta(days=20)}T18:00:00+00:00")   # TECH, last year
        base._entry(db, clock_in=f"{today - timedelta(days=2)}T18:00:00+00:00")    # TECH, this year
        db.commit()
        _policy(db, ANNIVERSARY)
    window = vacation_window(ANNIVERSARY, hired, today)

    mine = tech.get("/api/timeclock/time-off/balances")
    assert mine.status_code == 200, mine.text
    row = mine.json()["balances"][0]
    assert (row["window_start"], row["window_end"]) == (window[0].isoformat(), window[1].isoformat())
    assert row["used_hours"] == 8.0 and row["year"] == window[0].year

    base._as(tech, OFFICE, "dispatcher")
    names = [b["technician_name"] for b in
             tech.get("/api/timeclock/time-off/balances?all_technicians=true").json()["balances"]]
    assert "Former Tech" not in names and "Amber Rosa" in names

    # An explicit past year reads the vacation year in force on its Dec 31.
    last = tech.get(f"/api/timeclock/time-off/balances?technician_id={TECH}&year={today.year - 1}").json()
    dec31 = vacation_window(ANNIVERSARY, hired, date(today.year - 1, 12, 31))
    assert last["balances"][0]["window_start"] == dec31[0].isoformat()


# ── calendar-year mode is unchanged, byte for byte ───────────────────────────

# Captured from c2f5c66c (before GDXA-1855) by building these two sheets with
# a hire date set on TECH, which the calendar year must ignore.
GOLDEN_CSV = {
    PERIOD.start: (
        "employee,employee_id,period_start,period_end,date,type,clock_in,clock_out,break_minutes,worked_hours,"
        "time_off_hours,needs_review,note,vacation_year,vacation_allowance_hours,vacation_used_hours,"
        "vacation_remaining_hours,vacation_booked_after_period_hours\n"
        "Amber Rosa,TECH,2026-08-10,2026-08-23,2026-08-17,shift,8:00 AM,8:00 AM,0,8.00,,,,,,,,\n"
        "Amber Rosa,TECH,2026-08-10,2026-08-23,2026-08-18,vacation,,,0,,8.00,,,,,,,\n"
        "Amber Rosa,TECH,2026-08-10,2026-08-23,TOTAL,,,,0,8.00,8.00,,\"1 shifts, 1 day off\",2026,40.00,16.00,24.00,16.00\n"
        "Michael T,OTHER,2026-08-10,2026-08-23,2026-08-17,shift,8:00 AM,8:00 AM,0,8.00,,,,,,,,\n"
        "Michael T,OTHER,2026-08-10,2026-08-23,TOTAL,,,,0,8.00,,,1 shifts,2026,80.00,0.00,80.00,0.00\n"
    ),
    date(2026, 12, 21): (
        "employee,employee_id,period_start,period_end,date,type,clock_in,clock_out,break_minutes,worked_hours,"
        "time_off_hours,needs_review,note,vacation_year,vacation_allowance_hours,vacation_used_hours,"
        "vacation_remaining_hours,vacation_booked_after_period_hours\n"
        "Amber Rosa,TECH,2026-12-21,2027-01-03,2026-12-28,vacation,,,0,,8.00,,,,,,,\n"
        "Amber Rosa,TECH,2026-12-21,2027-01-03,TOTAL,,,,0,0.00,8.00,,\"0 shifts, 1 day off\",2027,40.00,0.00,40.00,0.00\n"
    ),
}
GOLDEN_HTML_SHA256 = {
    PERIOD.start: "7107521ad1fd2118a8f09109c8be1680e8b7290048abedf8261c81a9f202fd01",
    date(2026, 12, 21): "1f0a48ee732a52d78ff24061202057756c7f3e48de2c37137a798b1f6f94be51",
}


def test_calendar_year_mode_writes_the_same_bytes_as_before(tech):
    db = base._sheet_db(tech)
    try:
        _hire(db, TECH, date(2019, 6, 15))
        base._entry(db, clock_in="2026-12-28T14:00:00+00:00")
        db.commit()
        for period in (PERIOD, PayPeriod(date(2026, 12, 21), date(2027, 1, 3))):
            sheet = _sheet(db, period)
            csv_text = build_csv(sheet).replace(TECH, "TECH").replace(OTHER, "OTHER")
            assert csv_text == GOLDEN_CSV[period.start]
            digest = hashlib.sha256(_html(sheet).encode()).hexdigest()
            assert digest == GOLDEN_HTML_SHA256[period.start], period
    finally:
        db.close()
