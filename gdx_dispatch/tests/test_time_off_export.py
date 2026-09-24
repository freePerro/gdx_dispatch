"""Paid time off in the pay-period files and on the settings surface.

* the CSV lists a vacation day with its own type and hours column, and the
  TOTAL row keeps worked hours apart from time off — folding them together
  is how a bookkeeper applies overtime to a day nobody worked;
* the PDF and the email carry the shop's "counts toward overtime" setting as
  a sentence; the CSV carries no such column (it is an hours file);
* a lunch taken on a day that also carries a posted holiday nets off the
  worked shift, never the holiday;
* the settings PATCH validates the calendar and the GET returns it.
"""
from __future__ import annotations

import csv
import io
from collections.abc import Generator
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.pay_periods import PayPeriod
from gdx_dispatch.core.timesheet_delivery import _body_html
from gdx_dispatch.core.timesheet_export import CSV_HEADER, build_csv, pdf_context
from gdx_dispatch.core.timesheet_hours import build_timesheet
from gdx_dispatch.models.tenant_models import AppSettings, TimeclockBreak, TimeclockEntry
from gdx_dispatch.tests.test_silent_success_sweep import _build_client, _login, _teardown

TENANT = "tenant-test"
TZ = "America/Chicago"
MICHAEL = "user-michael"
PERIOD = PayPeriod(date(2026, 8, 10), date(2026, 8, 23))
NAMES = {MICHAEL: "Michael Tallman"}


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


def _entry(db, *, entry_id, clock_in, clock_out, minutes, entry_type="clock", notes=None, tech=MICHAEL):
    db.add(TimeclockEntry(
        id=entry_id, tenant_id=TENANT, technician_id=tech,
        clock_in_at=clock_in, clock_out_at=clock_out, minutes=minutes,
        notes=notes, entry_type=entry_type, created_at=clock_in, updated_at=clock_in,
    ))
    db.commit()


def _seed_week(db):
    # Mon Aug 17: worked 8h (13:00Z–21:00Z is 8am–4pm Chicago) with a 30-min lunch.
    _entry(db, entry_id="w1", clock_in="2026-08-17T13:00:00+00:00", clock_out="2026-08-17T21:00:00+00:00", minutes=480)
    db.add(TimeclockBreak(
        id="b1", tenant_id=TENANT, user_id=MICHAEL, type="lunch",
        started_at="2026-08-17T17:00:00+00:00", ended_at="2026-08-17T17:30:00+00:00",
        duration_minutes=30, created_at="2026-08-17T17:00:00+00:00",
    ))
    db.commit()
    # Tue Aug 18: vacation. Wed Aug 19: holiday AND a worked shift (came in anyway).
    _entry(db, entry_id="v1", clock_in="2026-08-18T13:00:00+00:00", clock_out="2026-08-18T21:00:00+00:00",
           minutes=480, entry_type="vacation", notes="beach")
    _entry(db, entry_id="h1", clock_in="2026-08-19T13:00:00+00:00", clock_out="2026-08-19T21:00:00+00:00",
           minutes=480, entry_type="holiday", notes="Shop Day")
    _entry(db, entry_id="w2", clock_in="2026-08-19T14:00:00+00:00", clock_out="2026-08-19T18:00:00+00:00", minutes=240)
    db.add(TimeclockBreak(
        id="b2", tenant_id=TENANT, user_id=MICHAEL, type="lunch",
        started_at="2026-08-19T16:00:00+00:00", ended_at="2026-08-19T16:15:00+00:00",
        duration_minutes=15, created_at="2026-08-19T16:00:00+00:00",
    ))
    db.commit()


def _rows(text_csv: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text_csv)))


def test_csv_keeps_time_off_apart_from_worked_hours(db):
    _seed_week(db)
    sheet = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ, names=NAMES)
    rows = _rows(build_csv(sheet))
    assert rows[0]["type"] == "shift" and rows[0]["worked_hours"] == "7.50" and rows[0]["time_off_hours"] == ""
    vac = next(r for r in rows if r["type"] == "vacation")
    assert vac["date"] == "2026-08-18"
    assert vac["clock_in"] == "" and vac["clock_out"] == "", "a synthetic span prints no clock stamps"
    assert vac["worked_hours"] == "" and vac["time_off_hours"] == "8.00"
    assert vac["note"] == "beach" and vac["needs_review"] == ""
    hol = next(r for r in rows if r["type"] == "holiday")
    assert hol["time_off_hours"] == "8.00" and hol["break_minutes"] == "0"

    total = next(r for r in rows if r["date"] == "TOTAL")
    assert total["worked_hours"] == "11.25", "7.5 + 3.75 worked; the two days off are not in here"
    assert total["time_off_hours"] == "16.00"
    assert total["note"] == "2 shifts, 2 days off"
    assert total["needs_review"] == ""


def test_the_lunch_on_the_holiday_nets_off_the_worked_shift_not_the_holiday(db):
    _seed_week(db)
    sheet = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ, names=NAMES)
    card = sheet.timecards[0]
    by_id = {s.entry_id: s for s in card.shifts}
    assert by_id["h1"].break_minutes == 0 and by_id["h1"].time_off_hours == 8.0
    assert by_id["w2"].break_minutes == 15 and by_id["w2"].worked_hours == 3.75
    assert card.worked_hours == 11.25 and card.time_off_hours == 16.0 and card.paid_hours == 27.25
    assert sheet.worked_hours == 11.25 and sheet.time_off_hours == 16.0


def test_csv_header_has_the_split_and_still_no_money_words(db):
    header = ",".join(CSV_HEADER)
    assert "type" in CSV_HEADER and "time_off_hours" in CSV_HEADER
    for word in ("rate", "gross", "pay", "wage", "overtime", "amount"):
        assert word not in header.lower()


def test_pdf_and_email_state_the_overtime_setting_only_when_time_off_exists(db):
    _seed_week(db)
    off = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ, names=NAMES)
    ctx = pdf_context(off)
    assert ctx["total_hours"] == 11.25 and ctx["total_time_off_hours"] == 16.0 and ctx["total_paid_hours"] == 27.25
    assert ctx["time_off_statement"] == "Paid time off does not count toward overtime."
    card = ctx["cards"][0]
    assert card["hours"] == 11.25 and card["time_off_hours"] == 16.0 and card["time_off_days"] == 2
    vac_row = next(r for r in card["rows"] if r["type_label"] == "Vacation")
    assert vac_row["is_time_off"] and vac_row["clock_in"] == "" and vac_row["hours"] == 8.0
    html = _body_html(off, {"company_name": "Shop"}, "2026-08-28")
    assert "16.00 time off" in html and "does not count toward overtime" in html

    on = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ, names=NAMES,
                         time_off_counts_toward_overtime=True)
    assert pdf_context(on)["time_off_statement"] == "Paid time off counts toward overtime."
    assert "counts toward overtime" in _body_html(on, {"company_name": "Shop"}, "")

    # No time off in the period → no sentence at all.
    with db.begin_nested():
        pass
    for row in db.execute(select(TimeclockEntry).where(TimeclockEntry.entry_type != "clock")).scalars():
        row.deleted_at = "2026-08-20T00:00:00+00:00"
    db.commit()
    none = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ, names=NAMES)
    assert none.time_off_hours == 0 and pdf_context(none)["time_off_statement"] == ""
    assert "overtime" not in _body_html(none, {"company_name": "Shop"}, "")


def test_pdf_renders_a_period_with_time_off_for_real(db):
    from gdx_dispatch.core.timesheet_export import build_pdf

    _seed_week(db)
    sheet = build_timesheet(db, tenant_id=TENANT, period=PERIOD, tz_name=TZ, names=NAMES)
    try:
        pdf = build_pdf(sheet, branding={"company_name": "Shop"}, pay_date="2026-08-28", prepared_at="now")
    except ImportError:
        pytest.skip("WeasyPrint not installed here")
    assert pdf[:4] == b"%PDF" and len(pdf) > 1000


# ── settings ─────────────────────────────────────────────────────────────────


ADMIN = str(uuid4())


@pytest.fixture()
def settings_client():
    from gdx_dispatch.routers import settings as settings_mod

    c = _build_client(settings_mod.router, user=_login(ADMIN, "admin"))
    yield c
    _teardown(c)


def test_settings_round_trip_the_calendar_and_the_two_options(settings_client):
    c = settings_client
    r = c.get("/api/settings")
    assert r.status_code == 200, r.text
    assert r.json()["holiday_calendar"] == []
    assert r.json()["time_off_default_minutes"] == 480
    assert r.json()["time_off_counts_toward_overtime"] is False

    r = c.patch("/api/settings", json={
        "holiday_calendar": [
            {"date": "2026-12-25", "name": " Christmas Day ", "minutes": 480},
            {"date": "2026-11-26", "name": "Thanksgiving", "minutes": 480},
        ],
        "time_off_default_minutes": 420,
        "time_off_counts_toward_overtime": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert [h["date"] for h in body["holiday_calendar"]] == ["2026-11-26", "2026-12-25"]
    assert body["holiday_calendar"][1]["name"] == "Christmas Day"
    assert body["time_off_default_minutes"] == 420
    assert body["time_off_counts_toward_overtime"] is True
    with c.SessionLocal() as db:
        row = db.execute(select(AppSettings)).scalars().one()
        assert row.time_off_default_minutes == 420 and row.time_off_counts_toward_overtime is True
        assert [h["name"] for h in row.holiday_calendar] == ["Thanksgiving", "Christmas Day"]
        trail = db.execute(select(AuditLog).where(AuditLog.action == "settings_updated")).scalars().all()
        assert trail and trail[-1].details["time_off_default_minutes"] == 420
        assert trail[-1].details["holiday_calendar"][0]["date"] == "2026-11-26"
    assert c.get("/api/settings").json()["holiday_calendar"] == body["holiday_calendar"]


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ({"holiday_calendar": [{"date": "2026-13-01", "name": "X"}]}, "not a date"),
        ({"holiday_calendar": [{"date": "2026-12-25", "name": ""}]}, "needs a name"),
        ({"holiday_calendar": [{"date": "2026-12-25", "name": "A"}, {"date": "2026-12-25", "name": "B"}]}, "two holidays"),
        ({"holiday_calendar": [{"date": "2026-12-25", "name": "A", "minutes": 5}]}, "between"),
        ({"time_off_default_minutes": 5}, ""),
        ({"time_off_default_minutes": 5000}, ""),
    ],
)
def test_settings_refuse_a_bad_calendar_or_day_length(settings_client, payload, fragment):
    r = settings_client.patch("/api/settings", json=payload)
    assert r.status_code == 422, r.text
    if fragment:
        assert fragment in r.json()["detail"]
    assert settings_client.get("/api/settings").json()["holiday_calendar"] == []
