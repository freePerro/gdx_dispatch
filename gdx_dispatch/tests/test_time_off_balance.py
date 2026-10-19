"""Vacation used / allowance / remaining per person per calendar year (GDXA-701).

Every assertion reads real rows through the real code. Pinned, each named for
the failure it prevents:

* the person's override beats the shop default; neither set reads "no
  allowance" (None), never 0 — zero hours left and "nobody set a number" are
  different facts;
* only live, closed `vacation` entries are drawn from the allowance — a
  holiday, a soft-deleted entry, a worked shift and another year's day are not;
* the year is the SHOP's calendar year: a vacation day stored as 03:00Z on
  Jan 1 is Dec 31 in Chicago and belongs to the old year;
* approved days still ahead are counted as used (they are booked) and shown
  apart as `scheduled`; an overdrawn balance goes negative, never clamps;
* the read endpoint: me by default; someone else or everyone is the office's;
  a viewer reads nothing; a failed read is a 503, not "0 used";
* the CSV's TOTAL row carries the balance and the detail rows do not; a
  failed balance read prints "unavailable" and the hours still go out;
* the rendered PDF (GDXA-702) prints the same cut as the CSV under each
  person, says "over allowance" rather than a negative, "not set" rather than
  0.00, and "unavailable" rather than nothing.
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import OperationalError

from gdx_dispatch.core import time_off as time_off_rules
from gdx_dispatch.core.pay_periods import PayPeriod, shop_today
from gdx_dispatch.core.time_off import vacation_balances
from gdx_dispatch.core.timesheet_export import CSV_HEADER, build_csv, pdf_context
from gdx_dispatch.core.timesheet_hours import build_timesheet
from gdx_dispatch.models.tenant_models import AppSettings, TimeclockEntry, User
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests import test_payroll_timesheet_task as task_tests
from gdx_dispatch.tests.test_silent_success_sweep import TENANT, _build_client, _login, _teardown

# The beat task's own fixture, bound by name so pytest collects it here.
session_factory = task_tests.session_factory

TZ = "America/Chicago"
TECH = str(uuid4())
OTHER = str(uuid4())
GONE = str(uuid4())
OFFICE = str(uuid4())
PERIOD = PayPeriod(date(2026, 8, 10), date(2026, 8, 23))


def _client(role: str = "technician", user_id: str = TECH):
    from gdx_dispatch.routers import time_off as mod

    return _build_client(mod.router, user=_login(user_id, role), module_keys=("timeclock",))


def _as(client, user_id: str, role: str) -> None:
    client.app.dependency_overrides[get_current_user] = lambda: _login(user_id, role)


def _entry(db, *, tech=TECH, clock_in, minutes=480, entry_type="vacation", closed=True, deleted=False):
    eid = str(uuid4())
    db.add(TimeclockEntry(
        id=eid, tenant_id=TENANT, technician_id=tech, clock_in_at=clock_in,
        clock_out_at=clock_in if closed else None, minutes=minutes if closed else None,
        entry_type=entry_type, created_at=clock_in, updated_at=clock_in,
        deleted_at=clock_in if deleted else None,
    ))
    return eid


def _seed(db, *, shop_hours="80", tech_hours=None):
    db.add(AppSettings(
        company_name="Shop", timezone=TZ, default_workdays=31,
        vacation_allowance_hours=None if shop_hours is None else Decimal(shop_hours),
    ))
    db.add(User(id=UUID(TECH), company_id=TENANT, full_name="Amber Rosa", role="technician",
                vacation_allowance_hours=None if tech_hours is None else Decimal(tech_hours)))
    db.add(User(id=UUID(OTHER), company_id=TENANT, full_name="Michael T", role="technician"))
    db.add(User(id=UUID(OFFICE), company_id=TENANT, full_name="Office", role="dispatcher"))
    db.commit()


@pytest.fixture()
def tech():
    c = _client()
    yield c
    _teardown(c)


def _settings(db):
    return db.query(AppSettings).first()


# ── core ─────────────────────────────────────────────────────────────────────


def test_override_beats_shop_default_and_unset_is_none_not_zero(tech):
    with tech.SessionLocal() as db:
        _seed(db, shop_hours="80", tech_hours="40.5")
        out = vacation_balances(db, _settings(db), {TECH, OTHER}, on=date(2026, 6, 1), as_of=date(2026, 6, 1))
        assert (out[TECH].allowance_hours, out[TECH].allowance_source) == (Decimal("40.50"), "person")
        assert (out[OTHER].allowance_hours, out[OTHER].allowance_source) == (Decimal("80.00"), "shop")
        assert out[TECH].remaining_hours == 40.5

        _settings(db).vacation_allowance_hours = None
        db.commit()
        out = vacation_balances(db, _settings(db), {OTHER}, on=date(2026, 6, 1), as_of=date(2026, 6, 1))
        assert out[OTHER].allowance_hours is None and out[OTHER].allowance_source is None
        assert out[OTHER].remaining_hours is None and out[OTHER].as_dict()["remaining_hours"] is None


def test_a_zero_override_is_zero_hours_not_the_shop_default(tech):
    with tech.SessionLocal() as db:
        _seed(db, shop_hours="80", tech_hours="0")
        bal = vacation_balances(db, _settings(db), {TECH}, on=date(2026, 6, 1), as_of=date(2026, 6, 1))[TECH]
        assert (bal.allowance_hours, bal.allowance_source, bal.remaining_hours) == (Decimal("0.00"), "person", 0.0)


def test_only_live_closed_vacation_in_the_shop_year_is_used(tech):
    with tech.SessionLocal() as db:
        _seed(db)
        _entry(db, clock_in="2026-03-02T14:00:00+00:00")                        # counts: 8h
        _entry(db, clock_in="2026-03-03T14:00:00+00:00", minutes=240)           # counts: 4h
        _entry(db, clock_in="2026-07-03T13:00:00+00:00", entry_type="holiday")  # holiday: no
        _entry(db, clock_in="2026-03-04T14:00:00+00:00", entry_type="clock")    # worked: no
        _entry(db, clock_in="2026-03-05T14:00:00+00:00", deleted=True)          # deleted: no
        _entry(db, clock_in="2026-03-06T14:00:00+00:00", closed=False)          # open: no
        _entry(db, clock_in="2025-12-30T14:00:00+00:00")                        # last year: no
        # 03:00Z Jan 1 2027 is 9pm Dec 31 2026 in Chicago — this year.
        _entry(db, clock_in="2027-01-01T03:00:00+00:00", minutes=60)
        # 07:00Z Jan 1 2026 is 1am Jan 1 in Chicago — this year too.
        _entry(db, clock_in="2026-01-01T07:00:00+00:00", minutes=30)
        _entry(db, tech=OTHER, clock_in="2026-03-02T14:00:00+00:00")            # someone else
        db.commit()
        bal = vacation_balances(db, _settings(db), {TECH}, on=date(2026, 12, 31), as_of=date(2026, 12, 31))[TECH]
        assert bal.used_minutes == 480 + 240 + 60 + 30
        assert bal.used_hours == 13.5 and bal.remaining_hours == 66.5
        last = vacation_balances(db, _settings(db), {TECH}, on=date(2025, 12, 31), as_of=date(2026, 12, 31))[TECH]
        assert last.used_minutes == 480, "Dec 30 2025 only — the Jan 1 03:00Z day is 2026's"


def test_booked_days_ahead_are_used_and_shown_as_scheduled_and_overdraw_goes_negative(tech):
    with tech.SessionLocal() as db:
        _seed(db, tech_hours="16")
        _entry(db, clock_in="2026-05-04T13:00:00+00:00")
        _entry(db, clock_in="2026-09-14T13:00:00+00:00")
        _entry(db, clock_in="2026-09-15T13:00:00+00:00")
        db.commit()
        bal = vacation_balances(db, _settings(db), {TECH}, on=date(2026, 8, 23), as_of=date(2026, 8, 23))[TECH]
        d = bal.as_dict()
        assert (d["used_hours"], d["taken_hours"], d["scheduled_hours"]) == (24.0, 8.0, 16.0)
        assert d["remaining_hours"] == -8.0 and d["overdrawn"] is True
        assert d["allowance_hours"] == 16.0 and d["year"] == 2026 and d["as_of"] == "2026-08-23"


def test_odd_minutes_round_to_the_cent_hour_once(tech):
    with tech.SessionLocal() as db:
        _seed(db, tech_hours="10")
        for day in (2, 3, 4):
            _entry(db, clock_in=f"2026-03-0{day}T14:00:00+00:00", minutes=35)
        db.commit()
        bal = vacation_balances(db, _settings(db), {TECH}, on=date(2026, 12, 31), as_of=date(2026, 12, 31))[TECH]
        # 105 min = 1.75h exactly; three separately rounded 0.58h would be 1.74.
        assert bal.used_hours == 1.75 and bal.remaining_hours == 8.25


# ── endpoint ─────────────────────────────────────────────────────────────────


def _this_year_stamp(days_from_today: int) -> str:
    day = shop_today(TZ) + timedelta(days=days_from_today)
    return f"{day.isoformat()}T18:00:00+00:00"


def test_a_tech_reads_their_own_balance(tech):
    today = shop_today(TZ)
    with tech.SessionLocal() as db:
        _seed(db, tech_hours="40")
        _entry(db, clock_in=f"{today.year}-01-05T18:00:00+00:00")
        _entry(db, tech=OTHER, clock_in=f"{today.year}-01-05T18:00:00+00:00")
        db.commit()
    r = tech.get("/api/timeclock/time-off/balances")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["year"] == today.year and body["as_of"] == today.isoformat()
    assert body["shop_allowance_hours"] == 80.0
    assert [b["technician_id"] for b in body["balances"]] == [TECH]
    mine = body["balances"][0]
    assert mine["technician_name"] == "Amber Rosa"
    assert (mine["allowance_hours"], mine["allowance_source"]) == (40.0, "person")
    if today >= date(today.year, 1, 6):
        assert (mine["used_hours"], mine["remaining_hours"]) == (8.0, 32.0)

    r = tech.get(f"/api/timeclock/time-off/balances?technician_id={TECH}&year={today.year - 1}")
    assert r.status_code == 200 and r.json()["balances"][0]["used_hours"] == 0.0


def test_a_tech_cannot_read_anyone_else_and_a_viewer_reads_nothing(tech):
    with tech.SessionLocal() as db:
        _seed(db)
    assert tech.get(f"/api/timeclock/time-off/balances?technician_id={OTHER}").status_code == 403
    assert tech.get("/api/timeclock/time-off/balances?all_technicians=true").status_code == 403
    _as(tech, TECH, "viewer")
    assert tech.get("/api/timeclock/time-off/balances").status_code == 403


def test_the_office_reads_everyone_including_a_departed_person_with_vacation(tech):
    year = shop_today(TZ).year
    with tech.SessionLocal() as db:
        _seed(db)
        # Departed: soft-deleted user row, vacation this year — still listed.
        db.add(User(id=UUID(GONE), company_id=TENANT, full_name="Former Tech", role="technician",
                    deleted_at=shop_today(TZ)))
        _entry(db, tech=GONE, clock_in=f"{year}-01-05T18:00:00+00:00")
        # Inactive with no vacation: not listed.
        db.add(User(id=uuid4(), company_id=TENANT, full_name="Inactive", role="technician", active=False))
        db.commit()
    _as(tech, OFFICE, "dispatcher")
    r = tech.get("/api/timeclock/time-off/balances?all_technicians=true")
    assert r.status_code == 200, r.text
    names = [b["technician_name"] for b in r.json()["balances"]]
    assert names == ["Amber Rosa", "Former Tech", "Michael T", "Office"]
    one = tech.get(f"/api/timeclock/time-off/balances?technician_id={OTHER}").json()["balances"]
    assert [b["technician_id"] for b in one] == [OTHER]


def test_an_approved_request_draws_down_the_balance(tech):
    """The real path: tech asks, office approves, the balance moves."""
    with tech.SessionLocal() as db:
        _seed(db, tech_hours="40")
    day = shop_today(TZ) + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    r = tech.post("/api/timeclock/time-off/requests", json={
        "start_date": day.isoformat(), "end_date": day.isoformat(), "notes": "dentist",
    })
    assert r.status_code == 201, r.text
    req = r.json()
    before = tech.get("/api/timeclock/time-off/balances").json()["balances"][0]
    assert before["used_hours"] == 0.0, "a pending request is not used"
    _as(tech, OFFICE, "dispatcher")
    assert tech.post(f"/api/timeclock/time-off/requests/{req['id']}/approve", json={}).status_code == 200
    _as(tech, TECH, "technician")
    after = tech.get(f"/api/timeclock/time-off/balances?year={day.year}").json()["balances"][0]
    assert (after["used_hours"], after["scheduled_hours"], after["remaining_hours"]) == (8.0, 8.0, 32.0)


def test_a_failed_read_is_a_503_not_zero_used(tech, monkeypatch):
    with tech.SessionLocal() as db:
        _seed(db)
    from gdx_dispatch.routers import time_off as mod

    def boom(*_a, **_k):
        raise OperationalError("SELECT", {}, Exception("db gone"))

    monkeypatch.setattr(mod, "vacation_balances", boom)
    r = tech.get("/api/timeclock/time-off/balances")
    assert r.status_code == 503 and "could not be read" in r.json()["detail"]


# ── the pay-period files ─────────────────────────────────────────────────────


def _rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def _sheet_db(tech):
    db = tech.SessionLocal()
    _seed(db, tech_hours="40")
    _entry(db, clock_in="2026-03-02T14:00:00+00:00")                    # earlier in the year
    _entry(db, clock_in="2026-08-18T13:00:00+00:00")                    # in the period
    _entry(db, clock_in="2026-08-17T13:00:00+00:00", entry_type="clock")
    _entry(db, clock_in="2026-09-14T13:00:00+00:00")                    # after the period
    _entry(db, tech=OTHER, clock_in="2026-08-17T13:00:00+00:00", entry_type="clock")
    db.commit()
    return db


def test_the_csv_total_row_carries_the_years_balance_as_of_the_period_end(tech):
    db = _sheet_db(tech)
    try:
        sheet = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ,
                                names={TECH: "Amber Rosa", OTHER: "Michael T"},
                                with_vacation=True, settings=_settings(db))
        assert CSV_HEADER[-5:] == ["vacation_year", "vacation_allowance_hours",
                                   "vacation_used_hours", "vacation_remaining_hours",
                                   "vacation_booked_after_period_hours"]
        rows = _rows(build_csv(sheet))
        totals = {r["employee"]: r for r in rows if r["date"] == "TOTAL"}
        amber = totals["Amber Rosa"]
        # Through the period's last day: Mar 2 + Aug 18 = 16h; Sep 14 is booked after.
        assert (amber["vacation_year"], amber["vacation_allowance_hours"],
                amber["vacation_used_hours"], amber["vacation_remaining_hours"],
                amber["vacation_booked_after_period_hours"]) == ("2026", "40.00", "16.00", "24.00", "8.00")
        assert amber["time_off_hours"] == "8.00", "the period's hours are unchanged"
        mike = totals["Michael T"]
        assert (mike["vacation_allowance_hours"], mike["vacation_used_hours"],
                mike["vacation_remaining_hours"]) == ("80.00", "0.00", "80.00")
        for r in rows:
            if r["date"] != "TOTAL":
                assert r["vacation_used_hours"] == "" and r["vacation_year"] == ""

        ctx = pdf_context(sheet)
        assert ctx["vacation_year"] == 2026 and ctx["vacation_unreadable"] is False
        card = next(c for c in ctx["cards"] if c["name"] == "Amber Rosa")
        assert card["vacation"]["remaining_hours"] == 16.0
        assert card["vacation"]["scheduled_hours"] == 8.0, "Sep 14 is after the period's end"
        assert card["vacation"]["remaining_at_as_of_hours"] == 24.0
    finally:
        db.close()


def test_a_past_periods_csv_states_the_same_balance_after_a_later_booking(tech):
    db = _sheet_db(tech)
    try:
        def total():
            sheet = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ,
                                    names={TECH: "Amber Rosa"}, with_vacation=True, settings=_settings(db))
            row = next(r for r in _rows(build_csv(sheet)) if r["employee"] == "Amber Rosa" and r["date"] == "TOTAL")
            return row["vacation_used_hours"], row["vacation_remaining_hours"], row["vacation_booked_after_period_hours"]

        before = total()
        _entry(db, clock_in="2026-11-09T14:00:00+00:00")   # booked after the file first went out
        db.commit()
        after = total()
        assert after[:2] == before[:2] == ("16.00", "24.00")
        assert (before[2], after[2]) == ("8.00", "16.00")
    finally:
        db.close()


def test_no_allowance_reads_not_set_never_blank(tech):
    db = _sheet_db(tech)
    try:
        _settings(db).vacation_allowance_hours = None
        db.commit()
        sheet = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ,
                                names={OTHER: "Michael T"}, with_vacation=True, settings=_settings(db))
        mike = next(r for r in _rows(build_csv(sheet)) if r["employee"] == "Michael T" and r["date"] == "TOTAL")
        assert (mike["vacation_allowance_hours"], mike["vacation_used_hours"],
                mike["vacation_remaining_hours"]) == ("not set", "0.00", "not set")
    finally:
        db.close()


def test_a_failed_balance_read_says_unavailable_and_the_hours_still_go_out(tech, monkeypatch):
    db = _sheet_db(tech)
    try:
        def boom(*_a, **_k):
            raise OperationalError("SELECT", {}, Exception("db gone"))

        monkeypatch.setattr(time_off_rules, "vacation_balances", boom)
        sheet = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ,
                                names={TECH: "Amber Rosa"}, with_vacation=True, settings=_settings(db))
        assert sheet.vacation_unreadable is True
        amber = next(r for r in _rows(build_csv(sheet)) if r["employee"] == "Amber Rosa" and r["date"] == "TOTAL")
        assert amber["vacation_used_hours"] == "unavailable"
        assert amber["vacation_remaining_hours"] == "unavailable"
        assert amber["worked_hours"] == "8.00" and amber["time_off_hours"] == "8.00"
    finally:
        db.close()


def test_without_balances_the_columns_are_empty(tech):
    db = _sheet_db(tech)
    try:
        sheet = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ)
        assert sheet.vacation_year is None
        assert all(r["vacation_used_hours"] == "" for r in _rows(build_csv(sheet)))
    finally:
        db.close()


# ── the PDF (GDXA-702) ───────────────────────────────────────────────────────


def _pdf_text(sheet) -> str:
    """The rendered PDF's own text: WeasyPrint for real, read back by pypdf.
    Whitespace collapsed, since the layout decides where lines break."""
    from pypdf import PdfReader

    from gdx_dispatch.core.timesheet_export import build_pdf

    pdf = build_pdf(sheet, branding={"company_name": "Shop"}, pay_date="2026-08-28", prepared_at="now")
    text = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
    return " ".join(text.split())


def _person_block(text: str, name: str) -> str:
    """One person's part of the page: from their name to whichever comes
    first after it — another person's name or the footer. Order-agnostic."""
    start = text.index(name)
    ends = [i for m in ("Amber Rosa", "Michael T", "Worked hours are")
            if m != name and (i := text.find(m, start + len(name))) != -1]
    return text[start:min(ends)] if ends else text[start:]


def test_the_pdf_prints_each_persons_balance_as_the_csv_does(tech):
    db = _sheet_db(tech)
    try:
        sheet = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ,
                                names={TECH: "Amber Rosa", OTHER: "Michael T"},
                                with_vacation=True, settings=_settings(db))
        text = _pdf_text(sheet)
        amber = _person_block(text, "Amber Rosa")
        # Same cut as the CSV TOTAL row: 40 - (Mar 2 + Aug 18) = 24; Sep 14 is
        # booked after the period and is not drawn from this print.
        assert "Vacation 2026 through Aug 23, 2026:" in amber
        assert "40.00 allowance · 16.00 used · 24.00 remaining · 8.00 booked after this period" in amber
        mike = _person_block(text, "Michael T")
        assert "80.00 allowance · 0.00 used · 80.00 remaining" in mike
        assert "booked after" not in mike, "nothing booked ahead prints nothing"
        assert "nothing here pays out or carries over" in text

        totals = {r["employee"]: r for r in _rows(build_csv(sheet)) if r["date"] == "TOTAL"}
        a = totals["Amber Rosa"]
        assert (f"{a['vacation_allowance_hours']} allowance · {a['vacation_used_hours']} used · "
                f"{a['vacation_remaining_hours']} remaining") in amber, "the two files agree"
    finally:
        db.close()


def test_the_pdf_says_over_allowance_not_a_negative_or_zero(tech):
    db = _sheet_db(tech)
    try:
        db.get(User, UUID(TECH)).vacation_allowance_hours = Decimal("10")
        db.commit()
        sheet = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ,
                                names={TECH: "Amber Rosa", OTHER: "Michael T"}, with_vacation=True, settings=_settings(db))
        text = _pdf_text(sheet)
        assert "10.00 allowance · 16.00 used · 6.00 over allowance" in text
        assert "-6.00" not in text and "remaining" not in _person_block(text, "Amber Rosa")
    finally:
        db.close()


def test_the_pdf_says_not_set_when_no_allowance_exists(tech):
    db = _sheet_db(tech)
    try:
        _settings(db).vacation_allowance_hours = None
        db.commit()
        sheet = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ,
                                names={TECH: "Amber Rosa", OTHER: "Michael T"}, with_vacation=True, settings=_settings(db))
        mike = _person_block(_pdf_text(sheet), "Michael T")
        assert "allowance not set · 0.00 used" in mike
        assert "remaining" not in mike, "no allowance has no remainder — never 0.00"
    finally:
        db.close()


def test_the_pdf_says_unavailable_when_the_read_failed_and_still_prints_hours(tech, monkeypatch):
    db = _sheet_db(tech)
    try:
        def boom(*_a, **_k):
            raise OperationalError("SELECT", {}, Exception("db gone"))

        monkeypatch.setattr(time_off_rules, "vacation_balances", boom)
        sheet = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ,
                                names={TECH: "Amber Rosa", OTHER: "Michael T"}, with_vacation=True, settings=_settings(db))
        amber = _person_block(_pdf_text(sheet), "Amber Rosa")
        assert "Vacation 2026 · balance unavailable" in amber
        assert "8.00 hours worked · 8.00 paid time off" in amber
        assert "allowance" not in amber
    finally:
        db.close()


def test_the_pdf_warns_when_booked_days_take_someone_over(tech):
    """The Timesheets card counts booked days in `overdrawn`; the PDF's figure
    is through the period's end. Both must say "over" for the same person."""
    db = _sheet_db(tech)
    try:
        db.get(User, UUID(TECH)).vacation_allowance_hours = Decimal("20")
        db.commit()
        sheet = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ,
                                names={TECH: "Amber Rosa", OTHER: "Michael T"},
                                with_vacation=True, settings=_settings(db))
        assert sheet.timecards and next(c for c in sheet.timecards if c.tech_id == TECH).vacation.overdrawn
        amber = _person_block(_pdf_text(sheet), "Amber Rosa")
        assert ("20.00 allowance · 16.00 used · 4.00 remaining · 8.00 booked after this period · "
                "over allowance once booked days are taken") in amber
        mike = _person_block(_pdf_text(sheet), "Michael T")
        assert "over allowance" not in mike
    finally:
        db.close()


def test_a_period_crossing_new_year_says_the_old_years_days_are_not_in_the_balance(tech):
    db = tech.SessionLocal()
    try:
        _seed(db, tech_hours="40")
        _entry(db, clock_in="2026-12-28T14:00:00+00:00")
        db.commit()
        period = PayPeriod(date(2026, 12, 21), date(2027, 1, 3))
        sheet = build_timesheet(db, tenant_id=TENANT, period=period, tz_name=TZ,
                                names={TECH: "Amber Rosa", OTHER: "Michael T"},
                                with_vacation=True, settings=_settings(db))
        amber = _person_block(_pdf_text(sheet), "Amber Rosa")
        assert "Vacation 2027 through Jan 3, 2027: 40.00 allowance · 0.00 used" in amber
        assert "Vacation taken in 2026 is not counted in this balance." in amber
        inside = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ,
                                 names={TECH: "Amber Rosa", OTHER: "Michael T"},
                                 with_vacation=True, settings=_settings(db))
        assert "is not counted in this balance" not in _pdf_text(inside)
    finally:
        db.close()


def test_a_pdf_built_without_balances_prints_no_vacation_line(tech):
    db = _sheet_db(tech)
    try:
        text = _pdf_text(build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ))
        assert "Vacation 2026" not in text and "allowance" not in text
    finally:
        db.close()


# ── both callers that send the files ask for balances ────────────────────────


def test_the_office_csv_download_carries_the_balance_columns():
    from gdx_dispatch.tests import test_timesheet_export as export_tests

    with export_tests._export_app() as (tc, SessionLocal, _sessions):
        export_tests._seed(SessionLocal, entry_id="e1", clock_in="2026-08-17T13:00:00+00:00",
                           clock_out="2026-08-17T22:00:00+00:00", minutes=540)
        with SessionLocal() as db:
            db.query(AppSettings).first().vacation_allowance_hours = Decimal("80")
            db.add(TimeclockEntry(
                id="v-mike", tenant_id=export_tests.TENANT, technician_id=export_tests.MICHAEL,
                clock_in_at="2026-02-02T14:00:00+00:00", clock_out_at="2026-02-02T22:00:00+00:00",
                minutes=480, entry_type="vacation",
                created_at="2026-02-02T14:00:00+00:00", updated_at="2026-02-02T14:00:00+00:00",
            ))
            db.commit()
        r = tc.get("/api/timeclock/pay-period/export.csv?start=2026-08-10&end=2026-08-23")
        assert r.status_code == 200, r.text
        total = next(row for row in _rows(r.text) if row["date"] == "TOTAL")
        assert (total["vacation_year"], total["vacation_allowance_hours"],
                total["vacation_used_hours"], total["vacation_remaining_hours"]) == ("2026", "80.00", "8.00", "72.00")


def test_the_scheduled_send_attaches_a_csv_with_the_balance(session_factory, monkeypatch):
    import base64

    monkeypatch.setenv("GDX_TENANT_ID", task_tests.TENANT)
    task_tests._good_shift(session_factory)
    task_tests._settings(session_factory, vacation_allowance_hours=Decimal("40"))
    result, send = task_tests._run(session_factory)
    assert result.get("sent") is True, result
    csv_att = next(a for a in send.call_args.kwargs["attachments"] if a["content_type"] == "text/csv")
    text = base64.b64decode(csv_att["content_base64"]).decode("utf-8")
    total = next(row for row in _rows(text) if row["date"] == "TOTAL")
    assert (total["vacation_year"], total["vacation_allowance_hours"],
            total["vacation_used_hours"], total["vacation_remaining_hours"]) == ("2026", "40.00", "0.00", "40.00")
