"""An unposted holiday holds the pay-period send — manual and scheduled.

The Timesheets "not posted yet" notice only helps if a person looks before
the file goes. The scheduled send has no person in front of it, and once it
has sent a period it refuses to send it again — so the first Thanksgiving
after release would have mailed a file short a day for everyone. Pinned:

* the office's Send is refused (409, `unposted_holiday`) while a calendar
  holiday inside the range has no holiday entries, naming the holiday;
* posting the holiday for anyone clears the hold; a holiday outside the
  range never blocks;
* a flagged shift is reported BEFORE the holiday (it is the one the office
  is already looking at);
* the beat task holds the same way, tells the office once, and sends on the
  next tick after the holiday is posted.

Fixtures come from the two existing send test modules so the harness is the
one those tests already trust.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from sqlalchemy import text

from gdx_dispatch.core.timesheet_delivery import BLOCKED_FLAGGED, BLOCKED_HOLIDAY
from gdx_dispatch.models.tenant_models import TimeclockEntry
from gdx_dispatch.tests import test_payroll_timesheet_task as task_tests
from gdx_dispatch.tests import test_timesheet_send as send_tests

# Fixtures registered under these module attribute names — pytest collects a
# fixture object by the name it is bound to here. Assignments, not from-imports,
# so a test parameter of the same name is a plain shadow rather than an F811
# "redefinition of unused import".
client = send_tests.client
session_factory = task_tests.session_factory
_env = task_tests._env

MICHAEL = send_tests.MICHAEL
RANGE = send_tests.RANGE
SEND_PATH = send_tests.SEND_PATH
SEND_TARGET = send_tests.SEND_TARGET
TENANT = send_tests.TENANT
_good_shift = send_tests._good_shift
_settings = send_tests._settings
_shift = send_tests._shift

THANKSGIVING = {"date": "2026-08-20", "name": "Shop Holiday", "minutes": 480}
OUTSIDE = {"date": "2026-09-07", "name": "Labor Day", "minutes": 480}


def _holiday_entry(SessionLocal, *, entry_id="h-mike", tech=MICHAEL, day="2026-08-20"):
    session = SessionLocal()
    try:
        session.add(TimeclockEntry(
            id=entry_id, tenant_id=TENANT, technician_id=tech,
            clock_in_at=f"{day}T13:00:00+00:00", clock_out_at=f"{day}T21:00:00+00:00",
            minutes=480, notes="Shop Holiday", entry_type="holiday",
            created_at=f"{day}T13:00:00+00:00", updated_at=f"{day}T13:00:00+00:00",
        ))
        session.commit()
    finally:
        session.close()


def _blocked_audit(SessionLocal) -> list[str]:
    session = SessionLocal()
    try:
        return [
            str(r[0]) for r in session.execute(text(
                "SELECT details FROM audit_logs WHERE action = 'timesheet_send_blocked'"
            )).fetchall()
        ]
    finally:
        session.close()


# ── the office's Send button ─────────────────────────────────────────────────


def test_an_unposted_holiday_in_range_holds_the_send_and_names_it(client):
    tc, SessionLocal = client
    _good_shift(SessionLocal)
    _settings(SessionLocal, holiday_calendar=[THANKSGIVING])
    r = tc.post(SEND_PATH, json=RANGE)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["blocked"] == BLOCKED_HOLIDAY
    assert "Shop Holiday (2026-08-20)" in detail["detail"]
    assert detail["holidays"] == [THANKSGIVING]
    assert detail["flagged"] == []
    # Audited as a refusal, like every other hold.
    assert any("unposted_holiday" in d for d in _blocked_audit(SessionLocal))


def test_posting_the_holiday_for_anyone_clears_the_hold(client):
    tc, SessionLocal = client
    _good_shift(SessionLocal)
    _settings(SessionLocal, holiday_calendar=[THANKSGIVING])
    assert tc.post(SEND_PATH, json=RANGE).status_code == 409
    _holiday_entry(SessionLocal)
    with patch(SEND_TARGET, return_value=(True, "outlook_graph", None)):
        r = tc.post(SEND_PATH, json=RANGE)
    assert r.status_code == 200, r.text
    assert r.json()["sent"] is True
    assert r.json()["holidays"] == []


def test_a_holiday_outside_the_range_does_not_hold(client):
    tc, SessionLocal = client
    _good_shift(SessionLocal)
    _settings(SessionLocal, holiday_calendar=[OUTSIDE])
    with patch(SEND_TARGET, return_value=(True, "outlook_graph", None)):
        r = tc.post(SEND_PATH, json=RANGE)
    assert r.status_code == 200, r.text


def test_a_flagged_shift_is_reported_before_the_holiday(client):
    tc, SessionLocal = client
    _shift(SessionLocal, entry_id="e-open", clock_in="2026-08-11T13:00:00+00:00",
           clock_out=None, minutes=None)
    _settings(SessionLocal, holiday_calendar=[THANKSGIVING])
    detail = tc.post(SEND_PATH, json=RANGE).json()["detail"]
    assert detail["blocked"] == BLOCKED_FLAGGED
    assert detail["holidays"] == [], "the holiday list is filled only once the flags are clear"


# ── the scheduled send ───────────────────────────────────────────────────────


def test_the_beat_task_holds_on_an_unposted_holiday_and_tells_the_office_once(session_factory):
    task_tests._good_shift(session_factory)
    task_tests._settings(session_factory, holiday_calendar=[THANKSGIVING])

    held, send = task_tests._run(session_factory)
    assert held["held"] == BLOCKED_HOLIDAY, held
    assert send.call_count == 0
    notes = task_tests._notifications(session_factory)
    assert len(notes) == 1
    assert "Shop Holiday" in notes[0][1]

    # Next tick, nothing changed: still held, no second nag.
    again, _ = task_tests._run(session_factory, at=datetime(2026, 8, 24, 13, 30, tzinfo=task_tests.UTC))
    assert again["held"] == BLOCKED_HOLIDAY
    assert len(task_tests._notifications(session_factory)) == 1


def test_posting_the_holiday_makes_the_next_tick_send(session_factory):
    task_tests._good_shift(session_factory)
    task_tests._settings(session_factory, holiday_calendar=[THANKSGIVING])
    held, _ = task_tests._run(session_factory)
    assert held["held"] == BLOCKED_HOLIDAY

    db = session_factory()
    try:
        db.add(TimeclockEntry(
            id="h-mike", tenant_id=task_tests.TENANT, technician_id=task_tests.MICHAEL,
            clock_in_at="2026-08-20T13:00:00+00:00", clock_out_at="2026-08-20T21:00:00+00:00",
            minutes=480, notes="Shop Holiday", entry_type="holiday",
            created_at="2026-08-20T13:00:00+00:00", updated_at="2026-08-20T13:00:00+00:00",
        ))
        db.commit()
    finally:
        db.close()

    sent, send = task_tests._run(session_factory, at=datetime(2026, 8, 24, 15, 0, tzinfo=task_tests.UTC))
    assert sent.get("sent") is True, sent
    assert send.call_count == 1
