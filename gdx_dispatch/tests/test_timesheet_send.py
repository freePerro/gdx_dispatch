"""Sending the period's timesheet — and, mostly, refusing to.

The gate is the feature. These are named for what goes wrong without it:

* A period with an open shift is mailed. The bookkeeper pays what the file
  says, and somebody is short a day.
* "Sent" reported when nothing left the building. Every silent-success shape
  in this repo has cost real money; a send that reports true on a mail
  server rejection is the same shape.
* Partial delivery reported as plain success — two recipients, one bounced,
  and nobody has a reason to look.
* An override that lets a flagged period through anyway. There is none, and
  `test_there_is_no_override` is what keeps it that way.
* A refusal that leaves no trace. "Was the timesheet sent, by whom, and what
  stopped it" is a payroll question, so blocked sends are audited too.
"""
from __future__ import annotations

from collections.abc import Generator
from datetime import date
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.timesheet_delivery import (
    BLOCKED_EMPTY,
    BLOCKED_FLAGGED,
    BLOCKED_NO_RECIPIENT,
    BLOCKED_NO_SENDER,
)
from gdx_dispatch.models.tenant_models import AppSettings, TimeclockEntry
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.timeclock import router as timeclock_router

TENANT = "tenant-test"
TZ = "America/Chicago"
MICHAEL = "user-michael"
SEND_PATH = "/api/timeclock/pay-period/send"
RANGE = {"start": "2026-08-10", "end": "2026-08-23"}

#: Where send_transactional_email is LOOKED UP, not where it is defined —
#: timesheet_delivery imports it inside the function, so patching the source
#: module is what takes effect.
SEND_TARGET = "gdx_dispatch.core.transactional_email.send_transactional_email"


@pytest.fixture()
def client() -> Generator[tuple[TestClient, Any], None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    AppSettings.__table__.create(bind=engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = SessionLocal()
    for ddl in (
        """CREATE TABLE IF NOT EXISTS tenant_module_grants (
            id TEXT PRIMARY KEY, tenant_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS company_module_grants (
            id TEXT PRIMARY KEY, company_id TEXT, module_key TEXT,
            granted_at TEXT, created_at TEXT, expires_at TEXT,
            UNIQUE(company_id, module_key))""",
    ):
        setup.execute(text(ddl))
    setup.execute(text(
        "INSERT OR IGNORE INTO tenant_module_grants (id, tenant_id, module_key, granted_at, created_at)"
        " VALUES ('g1', 'tenant-test', 'timeclock', datetime('now'), datetime('now'))"
    ))
    setup.execute(text(
        "INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)"
        " VALUES ('g2', 'tenant-test', 'timeclock', datetime('now'), datetime('now'))"
    ))
    setup.add(AppSettings(
        company_name="Garage Door Xperts", address="", phone="", email="", logo="",
        timezone=TZ, enabled_modules=[], notification_preferences={}, integrations={},
        pay_period_cadence="biweekly", pay_period_anchor_start=date(2026, 8, 10),
        pay_period_pay_lag_days=5,
        payroll_recipient_emails="bookkeeper@example.com",
    ))
    setup.commit()
    setup.close()

    def _override_db():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(timeclock_router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "office-1", "sub": "office-1", "role": "dispatcher", "tenant_id": TENANT,
    }
    yield TestClient(app, raise_server_exceptions=True), SessionLocal
    app.dependency_overrides.clear()
    engine.dispose()


def _shift(SessionLocal, *, entry_id, clock_in, clock_out=None, minutes=480,
           tech=MICHAEL):
    session = SessionLocal()
    try:
        session.add(TimeclockEntry(
            id=entry_id, tenant_id=TENANT, technician_id=tech,
            clock_in_at=clock_in, clock_out_at=clock_out, minutes=minutes,
            notes=None, entry_type="clock", created_at=clock_in, updated_at=clock_in,
        ))
        session.commit()
    finally:
        session.close()


def _good_shift(SessionLocal):
    _shift(SessionLocal, entry_id="e-ok", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)


def _settings(SessionLocal, **kw):
    session = SessionLocal()
    try:
        row = session.query(AppSettings).first()
        for key, value in kw.items():
            setattr(row, key, value)
        session.commit()
    finally:
        session.close()


def _audit_actions(SessionLocal) -> list[tuple[str, str, str]]:
    session = SessionLocal()
    try:
        return [
            (r[0], r[1], str(r[2]))
            for r in session.execute(text(
                # NOT `LIKE 'timesheet_send%'` — that cannot match
                # 'timesheet_sent' (the 13th character is t, not d), so the
                # pattern silently returned nothing for every successful send.
                "SELECT action, user_id, details FROM audit_logs "
                "WHERE action IN ('timesheet_sent', 'timesheet_send_blocked')"
            )).fetchall()
        ]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_a_clean_period_is_sent(client):
    tc, SessionLocal = client
    _good_shift(SessionLocal)

    with patch(SEND_TARGET, return_value=(True, "outlook_graph", None)) as send:
        r = tc.post(SEND_PATH, json=RANGE)

    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is True
    assert body["delivered_to"] == ["bookkeeper@example.com"]
    assert body["hours"] == 9.0
    assert send.call_count == 1


def test_both_files_are_attached(client):
    """A PDF to read and a CSV to key in — and the PDF must really be one."""
    tc, SessionLocal = client
    _good_shift(SessionLocal)

    with patch(SEND_TARGET, return_value=(True, "outlook_graph", None)) as send:
        tc.post(SEND_PATH, json=RANGE)

    attachments = send.call_args.kwargs["attachments"]
    assert {a["content_type"] for a in attachments} == {"application/pdf", "text/csv"}
    assert all(a["name"].startswith("timesheet_2026-08-10_2026-08-23") for a in attachments)
    import base64
    pdf = next(a for a in attachments if a["content_type"] == "application/pdf")
    assert base64.b64decode(pdf["content_base64"]).startswith(b"%PDF")


def test_the_subject_names_the_period(client):
    tc, SessionLocal = client
    _good_shift(SessionLocal)

    with patch(SEND_TARGET, return_value=(True, "outlook_graph", None)) as send:
        tc.post(SEND_PATH, json=RANGE)

    subject = send.call_args.kwargs["subject"]
    assert "2026-08-10" in subject and "2026-08-23" in subject


def test_the_email_carries_no_pay_figures(client):
    """Hours only. A gross figure here would be pay this app was never told."""
    tc, SessionLocal = client
    _good_shift(SessionLocal)

    with patch(SEND_TARGET, return_value=(True, "outlook_graph", None)) as send:
        tc.post(SEND_PATH, json=RANGE)

    html = send.call_args.kwargs["html_body"].lower()
    for word in ("gross", "wage", "$"):
        assert word not in html


def test_every_recipient_gets_it(client):
    tc, SessionLocal = client
    _good_shift(SessionLocal)
    _settings(SessionLocal, payroll_recipient_emails="a@example.com, b@example.com")

    with patch(SEND_TARGET, return_value=(True, "outlook_graph", None)) as send:
        body = tc.post(SEND_PATH, json=RANGE).json()

    assert send.call_count == 2
    assert body["delivered_to"] == ["a@example.com", "b@example.com"]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_an_open_shift_holds_the_whole_send(client):
    tc, SessionLocal = client
    _good_shift(SessionLocal)
    _shift(SessionLocal, entry_id="e-open", clock_in="2026-08-20T13:00:00+00:00",
           clock_out=None, minutes=None)

    with patch(SEND_TARGET, return_value=(True, "outlook_graph", None)) as send:
        r = tc.post(SEND_PATH, json=RANGE)

    assert r.status_code == 409
    assert send.call_count == 0, "nothing may leave while a shift is unresolved"
    detail = r.json()["detail"]
    assert detail["blocked"] == BLOCKED_FLAGGED


def test_the_refusal_names_who_and_which_day(client):
    """A count sends the operator hunting. The list is the actionable part."""
    tc, SessionLocal = client
    _shift(SessionLocal, entry_id="e-unknown", clock_in="2026-08-18T13:00:00+00:00",
           clock_out="2026-08-18T22:00:00+00:00", minutes=None)

    detail = tc.post(SEND_PATH, json=RANGE).json()["detail"]
    assert len(detail["flagged"]) == 1
    flag = detail["flagged"][0]
    assert flag["date"] == "2026-08-18"
    assert flag["reason"]
    assert flag["entry_id"] == "e-unknown", "the screen needs this to open the fix"


def test_there_is_no_override(client):
    """The endpoint accepts start/end/technician_id and nothing else. A
    `force` that quietly worked would be the whole gate undone."""
    tc, SessionLocal = client
    _shift(SessionLocal, entry_id="e-open", clock_in="2026-08-20T13:00:00+00:00",
           clock_out=None, minutes=None)

    for override in ({"force": True}, {"send_anyway": True}, {"ignore_flags": True}):
        with patch(SEND_TARGET, return_value=(True, "outlook_graph", None)) as send:
            r = tc.post(SEND_PATH, json={**RANGE, **override})
        assert r.status_code in (409, 422), override
        assert send.call_count == 0, override


def test_correcting_the_shift_clears_the_hold(client):
    """The fix IS the dismissal — the same contract the exceptions card uses.
    Without this, the gate would be a dead end."""
    tc, SessionLocal = client
    _good_shift(SessionLocal)
    _shift(SessionLocal, entry_id="e-open", clock_in="2026-08-20T13:00:00+00:00",
           clock_out=None, minutes=None)
    assert tc.post(SEND_PATH, json=RANGE).status_code == 409

    session = SessionLocal()
    try:
        entry = session.get(TimeclockEntry, "e-open")
        entry.clock_out_at = "2026-08-20T21:00:00+00:00"
        entry.minutes = 480
        session.commit()
    finally:
        session.close()

    with patch(SEND_TARGET, return_value=(True, "outlook_graph", None)):
        assert tc.post(SEND_PATH, json=RANGE).status_code == 200


def test_a_long_but_possible_day_does_not_hold_the_send(client):
    """A real 15-hour day must go. If it did not, the only way to send would
    be to edit true data into false data."""
    tc, SessionLocal = client
    _shift(SessionLocal, entry_id="e-15h", clock_in="2026-08-18T11:00:00+00:00",
           clock_out="2026-08-19T02:00:00+00:00", minutes=15 * 60)

    with patch(SEND_TARGET, return_value=(True, "outlook_graph", None)):
        assert tc.post(SEND_PATH, json=RANGE).status_code == 200


def test_no_recipient_is_a_refusal_not_a_silent_no_op(client):
    tc, SessionLocal = client
    _good_shift(SessionLocal)
    _settings(SessionLocal, payroll_recipient_emails="")

    r = tc.post(SEND_PATH, json=RANGE)
    assert r.status_code == 409
    assert r.json()["detail"]["blocked"] == BLOCKED_NO_RECIPIENT


def test_an_empty_period_is_not_mailed_as_zero_hours(client):
    tc, _ = client
    r = tc.post(SEND_PATH, json=RANGE)
    assert r.status_code == 409
    assert r.json()["detail"]["blocked"] == BLOCKED_EMPTY


def test_flags_are_reported_before_a_missing_recipient(client):
    """Both wrong at once: the operator is looking at the shifts, so name
    those. Reporting "no recipient" would send them to the wrong screen."""
    tc, SessionLocal = client
    _shift(SessionLocal, entry_id="e-open", clock_in="2026-08-20T13:00:00+00:00",
           clock_out=None, minutes=None)
    _settings(SessionLocal, payroll_recipient_emails="")

    assert tc.post(SEND_PATH, json=RANGE).json()["detail"]["blocked"] == BLOCKED_FLAGGED


# ---------------------------------------------------------------------------
# Delivery honesty
# ---------------------------------------------------------------------------

def test_a_rejected_send_does_not_report_success(client):
    tc, SessionLocal = client
    _good_shift(SessionLocal)

    with patch(SEND_TARGET, return_value=(False, None, "no_mail_connection")):
        r = tc.post(SEND_PATH, json=RANGE)

    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["sent"] is False
    assert detail["failed_to"] == ["bookkeeper@example.com"]
    assert "no_mail_connection" in detail["detail"]


def test_partial_delivery_names_who_missed_out(client):
    """Reported as sent — one did arrive — but with the failure listed, so
    there is a reason to look."""
    tc, SessionLocal = client
    _good_shift(SessionLocal)
    _settings(SessionLocal, payroll_recipient_emails="ok@example.com, bad@example.com")

    def _one_fails(**kwargs):
        if kwargs["to_email"] == "bad@example.com":
            return (False, None, "rejected")
        return (True, "outlook_graph", None)

    with patch(SEND_TARGET, side_effect=_one_fails):
        body = tc.post(SEND_PATH, json=RANGE).json()

    assert body["sent"] is True
    assert body["delivered_to"] == ["ok@example.com"]
    assert body["failed_to"] == ["bad@example.com"]


def test_one_exploding_recipient_does_not_lose_the_others(client):
    tc, SessionLocal = client
    _good_shift(SessionLocal)
    _settings(SessionLocal, payroll_recipient_emails="boom@example.com, ok@example.com")

    def _one_raises(**kwargs):
        if kwargs["to_email"] == "boom@example.com":
            raise RuntimeError("graph exploded")
        return (True, "outlook_graph", None)

    with patch(SEND_TARGET, side_effect=_one_raises):
        body = tc.post(SEND_PATH, json=RANGE).json()

    assert body["delivered_to"] == ["ok@example.com"]
    assert body["failed_to"] == ["boom@example.com"]


# ---------------------------------------------------------------------------
# Permission and trail
# ---------------------------------------------------------------------------

def test_a_technician_cannot_send_the_crews_hours(client):
    tc, SessionLocal = client
    _good_shift(SessionLocal)
    tc.app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "tech-9", "sub": "tech-9", "role": "technician", "tenant_id": TENANT,
    }
    with patch(SEND_TARGET, return_value=(True, "outlook_graph", None)) as send:
        r = tc.post(SEND_PATH, json=RANGE)
    assert r.status_code == 403
    assert send.call_count == 0


def test_a_send_records_who_did_it(client):
    tc, SessionLocal = client
    _good_shift(SessionLocal)

    with patch(SEND_TARGET, return_value=(True, "outlook_graph", None)):
        tc.post(SEND_PATH, json=RANGE)

    rows = _audit_actions(SessionLocal)
    assert [r[0] for r in rows] == ["timesheet_sent"]
    assert rows[0][1] == "office-1"
    assert "bookkeeper@example.com" in rows[0][2]


def test_a_refusal_is_recorded_too(client):
    """A block that leaves no trace is indistinguishable from nobody trying."""
    tc, SessionLocal = client
    _shift(SessionLocal, entry_id="e-open", clock_in="2026-08-20T13:00:00+00:00",
           clock_out=None, minutes=None)

    tc.post(SEND_PATH, json=RANGE)

    rows = _audit_actions(SessionLocal)
    assert [r[0] for r in rows] == ["timesheet_send_blocked"]
    assert BLOCKED_FLAGGED in rows[0][2]


# ---------------------------------------------------------------------------
# Whose mailbox it leaves from
# ---------------------------------------------------------------------------
# Found on prod 2026-08-26, not by any test above: the scheduled send passed
# its own label ("system:payroll-timesheet") as the sender. Outlook Graph
# authenticates as a specific person and skips itself when it cannot parse a
# user id, SMTP is not configured on this deployment, so every attempt failed
# with "no_email_provider_connected" while the schedule looked healthy.
#
# The tests above all patched send_transactional_email, so they proved the
# right ARGUMENTS were assembled and could never have caught an argument that
# was well-formed and wrong.

def test_the_office_send_goes_out_as_the_person_who_pressed_it(client):
    tc, SessionLocal = client
    _good_shift(SessionLocal)

    with patch(SEND_TARGET, return_value=(True, "outlook_graph", None)) as send:
        tc.post(SEND_PATH, json=RANGE)

    # office-1 is not a UUID in this fixture, so assert the wiring instead:
    # the sender is the caller, not a system label.
    assert send.call_args.kwargs["user_id"] == "office-1"
    assert send.call_args.kwargs["initiator_kind"] == "user"


def test_recipient_source_fits_the_column(client):
    """outbound_emails.recipient_source is varchar(20). A longer value aborts
    the INSERT, which poisons the session and takes the audit row with it —
    exactly what happened on prod."""
    tc, SessionLocal = client
    _good_shift(SessionLocal)

    with patch(SEND_TARGET, return_value=(True, "outlook_graph", None)) as send:
        tc.post(SEND_PATH, json=RANGE)

    assert len(send.call_args.kwargs["recipient_source"]) <= 20


def test_a_background_send_with_no_sender_refuses_instead_of_failing_forever():
    """An unattended send has no calling user. Without a nominated mailbox it
    can never deliver, so it must say so once rather than retry hourly."""
    from gdx_dispatch.core.pay_periods import PayPeriod
    from gdx_dispatch.core.timesheet_delivery import send_period_timesheet
    from gdx_dispatch.core.timesheet_hours import PeriodTimesheet, Shift, Timecard

    class _S:
        timezone = TZ
        pay_period_cadence = "biweekly"
        pay_period_anchor_start = date(2026, 8, 10)
        pay_period_pay_lag_days = 5
        payroll_recipient_emails = "bookkeeper@example.com"

    period = PayPeriod(date(2026, 8, 10), date(2026, 8, 23))
    card = Timecard(tech_id="u1", name="Someone", shifts=[
        Shift(entry_id="e1", day=date(2026, 8, 17), clock_in="2026-08-17T13:00:00+00:00",
              clock_out="2026-08-17T22:00:00+00:00", minutes=540, break_minutes=0,
              entry_type="clock", notes=None, flag=None),
    ])
    sheet = PeriodTimesheet(period=period, timezone=TZ, timecards=[card])

    with patch(SEND_TARGET) as send:
        outcome = send_period_timesheet(
            None, tenant_id=TENANT, settings=_S(), sheet=sheet,
            actor_user_id="system:payroll-timesheet", initiator_kind="system",
            sender_user_id="",
        )
    assert outcome.blocked == BLOCKED_NO_SENDER
    assert outcome.sent is False
    assert send.call_count == 0, "must not attempt a send it cannot deliver"
    assert "mailbox" in outcome.detail.lower()


def test_a_system_label_is_not_accepted_as_a_mailbox():
    """The precise prod bug: truthy, well-formed, and unusable."""
    from gdx_dispatch.core.timesheet_delivery import _looks_like_user_id

    assert _looks_like_user_id("system:payroll-timesheet") is False
    assert _looks_like_user_id("") is False
    assert _looks_like_user_id(None) is False
    assert _looks_like_user_id("1f23a32a-198e-4a2d-90b7-4998c845790e") is True
