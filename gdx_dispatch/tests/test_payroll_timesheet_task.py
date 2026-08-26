"""The scheduled payroll send.

This repo has been burned twice by beat entries that fired on schedule and
did nothing: a reminders task whose three helpers were stubs and logged
`succeeded {'scheduled_count': 0}` hourly for months, and a recurring-jobs
entry pointing at a task name that never existed. So the tests here are
weighted toward what the task DOESN'T do and why it says so:

* fires on the morning after close, not on payday morning
* refuses to send twice for the same period
* catches up after downtime instead of skipping a fortnight in silence
* stops retrying once payday has passed
* holds a period with a flagged shift, alerts ONCE, and sends by itself the
  moment the shift is corrected
* never runs at all when autosend is off — the default for every install

Every return value names a reason. `test_every_early_return_says_why` is
what keeps a bare `return` from creeping back in.
"""
from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import AppSettings, Notification, TimeclockEntry
from gdx_dispatch.tasks import payroll_timesheet as task_mod

TENANT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
TZ = "America/Chicago"
MICHAEL = "user-michael"
SEND_TARGET = "gdx_dispatch.core.transactional_email.send_transactional_email"

#: Doug's fortnight: Mon 8/10 – Sun 8/23, paid Fri 8/28. The send should run
#: Mon 8/24 at 07:00 shop time.
CLOSE_MORNING = datetime(2026, 8, 24, 12, 30, tzinfo=UTC)  # 07:30 America/Chicago
PERIOD_KEY = "2026-08-10..2026-08-23"


@pytest.fixture()
def session_factory() -> Generator:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    AppSettings.__table__.create(bind=engine, checkfirst=True)
    Notification.__table__.create(bind=engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = SessionLocal()
    db.add(AppSettings(
        company_name="Garage Door Xperts", address="", phone="", email="", logo="",
        timezone=TZ, enabled_modules=[], notification_preferences={}, integrations={},
        pay_period_cadence="biweekly",
        pay_period_anchor_start=date(2026, 8, 10),
        pay_period_pay_lag_days=5,
        payroll_recipient_emails="bookkeeper@example.com",
        payroll_autosend_enabled=True,
        payroll_autosend_hour=7,
        # WHOSE mailbox the unattended send leaves from. Without this the
        # task cannot deliver at all — the prod defect of 2026-08-26.
        automation_sender_user_id="1f23a32a-198e-4a2d-90b7-4998c845790e",
    ))
    db.commit()
    db.close()

    yield SessionLocal
    engine.dispose()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GDX_TENANT_ID", TENANT)


def _shift(SessionLocal, *, entry_id, clock_in, clock_out=None, minutes=480):
    db = SessionLocal()
    try:
        db.add(TimeclockEntry(
            id=entry_id, tenant_id=TENANT, technician_id=MICHAEL,
            clock_in_at=clock_in, clock_out_at=clock_out, minutes=minutes,
            notes=None, entry_type="clock", created_at=clock_in, updated_at=clock_in,
        ))
        db.commit()
    finally:
        db.close()


def _good_shift(SessionLocal):
    _shift(SessionLocal, entry_id="e-ok", clock_in="2026-08-17T13:00:00+00:00",
           clock_out="2026-08-17T22:00:00+00:00", minutes=540)


def _settings(SessionLocal, **kw):
    db = SessionLocal()
    try:
        row = db.query(AppSettings).first()
        for key, value in kw.items():
            setattr(row, key, value)
        db.commit()
    finally:
        db.close()


class _FrozenDatetime(datetime):
    """Only `now()` is frozen; every other datetime behavior is real."""

    _frozen = CLOSE_MORNING

    @classmethod
    def now(cls, tz=None):
        return cls._frozen.astimezone(tz) if tz else cls._frozen.replace(tzinfo=None)


def _run(SessionLocal, *, at=CLOSE_MORNING, send_result=(True, "outlook_graph", None),
         send_side_effect=None):
    """One beat tick at a chosen moment."""
    _FrozenDatetime._frozen = at
    kwargs = {"side_effect": send_side_effect} if send_side_effect else {"return_value": send_result}
    with patch.object(task_mod, "SessionLocal", SessionLocal), \
         patch.object(task_mod, "datetime", _FrozenDatetime), \
         patch(SEND_TARGET, **kwargs) as send:
        result = task_mod.send_closed_period()
    return result, send


def _notifications(SessionLocal) -> list[tuple[str, str]]:
    db = SessionLocal()
    try:
        return [
            (r[0], r[1])
            for r in db.execute(text("SELECT title, message FROM notifications")).fetchall()
        ]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The morning after
# ---------------------------------------------------------------------------

def test_it_sends_the_closed_period_the_morning_after(session_factory):
    _good_shift(session_factory)
    result, send = _run(session_factory)

    assert result["sent"] is True
    assert result["period"] == PERIOD_KEY, "the CLOSED fortnight, not the live one"
    assert send.call_count == 1


def test_it_sends_the_period_being_paid_not_the_one_being_worked(session_factory):
    """On 8/24 the live period is 8/24–9/6. Sending that would mail one day
    of hours and look entirely plausible."""
    _good_shift(session_factory)
    _shift(session_factory, entry_id="e-live", clock_in="2026-08-24T13:00:00+00:00",
           clock_out="2026-08-24T22:00:00+00:00", minutes=540)

    result, _ = _run(session_factory)
    assert result["period"] == PERIOD_KEY
    assert result["hours"] == 9.0, "only the closed fortnight's 9 hours"


def test_nothing_fires_before_the_configured_hour(session_factory):
    _good_shift(session_factory)
    # 10:30 UTC is 05:30 in Chicago — before the 07:00 setting.
    result, send = _run(session_factory, at=datetime(2026, 8, 24, 10, 30, tzinfo=UTC))

    assert result["skipped"] == "too_early"
    assert send.call_count == 0


def test_it_does_not_fire_on_the_last_day_of_the_period(session_factory):
    """Sunday 8/23 — clock-outs for that day have not happened yet."""
    _good_shift(session_factory)
    result, send = _run(session_factory, at=datetime(2026, 8, 23, 18, 0, tzinfo=UTC))

    # The most recently CLOSED period on 8/23 is the one before Doug's.
    assert result.get("period") != PERIOD_KEY
    assert send.call_count == 0 or result.get("sent") is not True


# ---------------------------------------------------------------------------
# Idempotence and catch-up
# ---------------------------------------------------------------------------

def test_it_does_not_send_the_same_period_twice(session_factory):
    """An hourly beat double-firing after a restart must not re-mail."""
    _good_shift(session_factory)
    first, send1 = _run(session_factory)
    assert first["sent"] is True

    second, send2 = _run(session_factory)
    assert second["skipped"] == "already_sent"
    assert send2.call_count == 0


def test_it_catches_up_after_downtime(session_factory):
    """The container was down all Monday. Tuesday must still send, not skip
    the fortnight in silence."""
    _good_shift(session_factory)
    result, send = _run(session_factory, at=datetime(2026, 8, 25, 14, 0, tzinfo=UTC))

    assert result["sent"] is True
    assert result["period"] == PERIOD_KEY
    assert send.call_count == 1


def test_it_still_sends_on_payday_itself(session_factory):
    _good_shift(session_factory)
    result, _ = _run(session_factory, at=datetime(2026, 8, 28, 14, 0, tzinfo=UTC))
    assert result["sent"] is True


def test_it_stops_retrying_once_payday_has_passed(session_factory):
    """After the money is paid a late file is a conversation, not a task
    that keeps firing."""
    _good_shift(session_factory)
    result, send = _run(session_factory, at=datetime(2026, 8, 29, 14, 0, tzinfo=UTC))

    assert result["skipped"] == "past_pay_date"
    assert send.call_count == 0


# ---------------------------------------------------------------------------
# The hold
# ---------------------------------------------------------------------------

def test_a_flagged_shift_holds_the_send_and_tells_the_office(session_factory):
    _good_shift(session_factory)
    _shift(session_factory, entry_id="e-unknown", clock_in="2026-08-18T13:00:00+00:00",
           clock_out="2026-08-18T22:00:00+00:00", minutes=None)

    result, send = _run(session_factory)

    assert result["held"] == "flagged_shifts"
    assert send.call_count == 0, "nothing may leave while a shift is unresolved"
    notes = _notifications(session_factory)
    assert len(notes) == 1
    assert "held" in notes[0][0].lower()


def test_the_alert_names_the_shift_to_fix(session_factory):
    """A bare count sends the operator hunting through a fortnight."""
    _shift(session_factory, entry_id="e-unknown", clock_in="2026-08-18T13:00:00+00:00",
           clock_out="2026-08-18T22:00:00+00:00", minutes=None)

    _run(session_factory)
    message = _notifications(session_factory)[0][1]
    assert "2026-08-18" in message
    assert "Timesheets" in message


def test_it_does_not_nag_every_hour(session_factory):
    """Twenty-four identical bell rows a day is how a real alert becomes
    wallpaper."""
    _shift(session_factory, entry_id="e-unknown", clock_in="2026-08-18T13:00:00+00:00",
           clock_out="2026-08-18T22:00:00+00:00", minutes=None)

    first, _ = _run(session_factory)
    second, _ = _run(session_factory, at=datetime(2026, 8, 24, 13, 30, tzinfo=UTC))

    assert first["notified"] is True
    assert second["notified"] is False
    assert len(_notifications(session_factory)) == 1


def test_correcting_the_shift_makes_the_next_tick_send(session_factory):
    """The whole reason the beat is hourly. Without this the office would
    have to remember a second button after fixing the hours."""
    _good_shift(session_factory)
    _shift(session_factory, entry_id="e-unknown", clock_in="2026-08-18T13:00:00+00:00",
           clock_out="2026-08-18T22:00:00+00:00", minutes=None)
    held, _ = _run(session_factory)
    assert held["held"] == "flagged_shifts"

    db = session_factory()
    try:
        entry = db.get(TimeclockEntry, "e-unknown")
        entry.minutes = 300
        db.commit()
    finally:
        db.close()

    sent, send = _run(session_factory, at=datetime(2026, 8, 24, 15, 0, tzinfo=UTC))
    assert sent["sent"] is True
    assert send.call_count == 1


# ---------------------------------------------------------------------------
# Off means off
# ---------------------------------------------------------------------------

def test_autosend_off_does_nothing_at_all(session_factory):
    _good_shift(session_factory)
    _settings(session_factory, payroll_autosend_enabled=False)

    result, send = _run(session_factory)
    assert result["skipped"] == "autosend_off"
    assert send.call_count == 0
    assert _notifications(session_factory) == []


def test_a_missing_tenant_id_skips_rather_than_guessing(session_factory, monkeypatch):
    """Sibling beat tasks fall back to a literal "gdx". Here that would query
    the wrong tenant, find no rows, and report an empty fortnight — the worst
    possible wrong answer."""
    _good_shift(session_factory)
    monkeypatch.delenv("GDX_TENANT_ID", raising=False)
    monkeypatch.delenv("GDX_DEFAULT_TENANT_ID", raising=False)

    result, send = _run(session_factory)
    assert result["skipped"] == "no_tenant"
    assert send.call_count == 0


def test_a_hand_broken_config_says_so_instead_of_guessing(session_factory):
    """Settings refuses to save biweekly with no anchor, so this means the
    row was edited by hand. Guessing a fortnight would mail two weeks
    nobody worked."""
    _good_shift(session_factory)
    _settings(session_factory, pay_period_anchor_start=None)

    result, send = _run(session_factory)
    assert result["skipped"] == "unconfigured"
    assert send.call_count == 0


# ---------------------------------------------------------------------------
# Failure honesty
# ---------------------------------------------------------------------------

def test_a_rejected_send_tells_the_office(session_factory):
    """An automation that fails silently is indistinguishable from one that
    never ran."""
    _good_shift(session_factory)
    result, _ = _run(session_factory, send_result=(False, None, "no_mail_connection"))

    assert "failed" in result
    notes = _notifications(session_factory)
    assert len(notes) == 1
    assert "not sent" in notes[0][0].lower() or "held" in notes[0][0].lower()


def test_a_failed_send_is_retried_next_tick(session_factory):
    """It must not be recorded as sent — nothing arrived."""
    _good_shift(session_factory)
    _run(session_factory, send_result=(False, None, "no_mail_connection"))

    result, send = _run(session_factory, at=datetime(2026, 8, 24, 15, 0, tzinfo=UTC))
    assert result["sent"] is True
    assert send.call_count == 1


def test_a_successful_send_is_announced(session_factory):
    _good_shift(session_factory)
    _run(session_factory)
    notes = _notifications(session_factory)
    assert len(notes) == 1
    assert "sent" in notes[0][0].lower()
    assert "bookkeeper@example.com" in notes[0][1]


def test_the_send_is_audited_as_nobody_pressing_anything(session_factory):
    _good_shift(session_factory)
    _run(session_factory)

    db = session_factory()
    try:
        rows = db.execute(text(
            "SELECT user_id, entity_id FROM audit_logs WHERE action = 'timesheet_sent'"
        )).fetchall()
    finally:
        db.close()
    assert rows
    assert rows[0][0] == task_mod.SYSTEM_ACTOR
    assert rows[0][1] == PERIOD_KEY


def test_every_early_return_says_why(session_factory):
    """A tick that returns nothing is a tick nobody can debug."""
    _settings(session_factory, payroll_autosend_enabled=False)
    result, _ = _run(session_factory)
    assert isinstance(result, dict) and result
    assert any(k in result for k in ("skipped", "held", "sent", "failed"))


# ---------------------------------------------------------------------------
# Whose mailbox the schedule sends from
# ---------------------------------------------------------------------------

def test_the_send_leaves_from_the_configured_mailbox_not_the_system_label(session_factory):
    """The prod defect, pinned. Outlook Graph authenticates as a specific
    person and skips itself when it cannot parse a user id. Passing
    SYSTEM_ACTOR as the sender made every send fail with
    "no_email_provider_connected" while the schedule reported healthy."""
    _good_shift(session_factory)
    _, send = _run(session_factory)

    sender = send.call_args.kwargs["user_id"]
    assert sender == "1f23a32a-198e-4a2d-90b7-4998c845790e"
    assert sender != task_mod.SYSTEM_ACTOR
    # ...while the AUDIT still records that nobody pressed anything.
    assert send.call_args.kwargs["initiator_ref"] == task_mod.SYSTEM_ACTOR
    assert send.call_args.kwargs["initiator_kind"] == "system"


def test_no_configured_mailbox_holds_and_says_so(session_factory):
    """Must not retry hourly against a provider that will never be chosen."""
    _good_shift(session_factory)
    _settings(session_factory, automation_sender_user_id=None)

    result, send = _run(session_factory)
    assert result["held"] == "no_sender"
    assert send.call_count == 0
    notes = _notifications(session_factory)
    assert len(notes) == 1
    assert "mailbox" in notes[0][1].lower()


def test_recipient_source_fits_its_column(session_factory):
    """outbound_emails.recipient_source is varchar(20); a longer value aborts
    the INSERT and poisons the session, losing the audit row with it."""
    _good_shift(session_factory)
    _, send = _run(session_factory)
    assert len(send.call_args.kwargs["recipient_source"]) <= 20
