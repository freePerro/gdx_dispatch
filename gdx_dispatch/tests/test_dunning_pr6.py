"""PR6-billing-capture (2026-07-07) — money actually gets chased.

Doug's decisions pinned:
1. Reminder emails ACTUALLY SEND (the endpoints only logged rows before —
   theater); delivery outcome is visible ([delivered]/[skipped: reason]).
2. Automated dunning is OPT-IN, default OFF; the beat task keys only off
   auto_send_enabled.
3. Idempotency = stored threshold_days — survives schedule edits; manual
   NULL-threshold logs never suppress the robot.
4. Per-invoice dunning_paused mutes real arrangements.
5. While off + not dismissed: Monday nudge with the live overdue picture;
   the dismiss is permanent.
6. At most ONE email per invoice per tick (highest crossed unsent
   threshold) — no triple-emailing on enable day.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.next_action import NextAction
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    Payment,
    PaymentReminder,
    ReminderSettings,
)
from gdx_dispatch.routers.invoice_reminders import (
    DunningPauseIn,
    SendReminderIn,
    _get_or_create_settings,
    auto_send_preview,
    compute_due_sends,
    send_reminder,
    set_dunning_pause,
)
from gdx_dispatch.tasks.invoice_reminders_auto import _run_auto_sends, _weekly_nudge

TENANT = "tenant-1"


@pytest.fixture
def tenant_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [
        Job.__table__,
        Customer.__table__,
        Invoice.__table__,
        InvoiceLine.__table__,
        Payment.__table__,
        PaymentReminder.__table__,
        ReminderSettings.__table__,
        NextAction.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _request() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    req.state.tenant_id = TENANT
    return req


def _user() -> dict[str, str]:
    return {"user_id": "office-1", "tenant_id": TENANT, "role": "admin"}


def _seed_customer(db, email: str | None = "amy@example.com") -> Customer:
    c = Customer(id=uuid4(), name="Amy Acme", email=email, company_id=TENANT)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _seed_overdue(db, customer, *, days: int, balance: float = 400.0, status: str = "sent") -> Invoice:
    inv = Invoice(
        company_id=TENANT,
        customer_id=customer.id,
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        billing_type="standard",
        sequence_number=1,
        subtotal=Decimal(str(balance)),
        tax_amount=Decimal("0"),
        total=Decimal(str(balance)),
        balance_due=Decimal(str(balance)),
        status=status,
        invoice_date=date.today() - timedelta(days=days + 10),
        due_date=date.today() - timedelta(days=days),
        public_token=uuid4().hex,
        locked=False,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


@pytest.fixture
def outbox(monkeypatch):
    """Capture transactional email sends."""
    sent: list[dict] = []

    def fake_send(**kw):
        sent.append(kw)
        return True, "smtp", None

    monkeypatch.setattr(
        "gdx_dispatch.core.transactional_email.send_transactional_email", fake_send
    )
    return sent


# --------------------------------------------------------------------------
# Manual reminder button actually sends
# --------------------------------------------------------------------------


def test_manual_email_reminder_actually_sends(tenant_db_session, outbox):
    db = tenant_db_session
    cust = _seed_customer(db)
    inv = _seed_overdue(db, cust, days=8)

    out = send_reminder(
        invoice_id=inv.id, request=_request(),
        payload=SendReminderIn(channel="email", stage="friendly"),
        user=_user(), db=db,
    )

    assert out["sent"] is True
    assert len(outbox) == 1
    assert outbox[0]["to_email"] == "amy@example.com"
    assert inv.invoice_number in outbox[0]["subject"]
    row = db.execute(select(PaymentReminder)).scalars().one()
    assert "[delivered]" in row.notes


def test_manual_reminder_without_email_surfaces_skip(tenant_db_session, outbox):
    db = tenant_db_session
    cust = _seed_customer(db, email=None)
    inv = _seed_overdue(db, cust, days=8)

    out = send_reminder(
        invoice_id=inv.id, request=_request(),
        payload=SendReminderIn(channel="email", stage="friendly"),
        user=_user(), db=db,
    )

    assert out["sent"] is False
    assert out["skip_reason"] == "no_recipient_email"
    assert outbox == []
    row = db.execute(select(PaymentReminder)).scalars().one()
    assert "[skipped: no_recipient_email]" in row.notes


def test_non_email_channels_stay_log_only(tenant_db_session, outbox):
    db = tenant_db_session
    cust = _seed_customer(db)
    inv = _seed_overdue(db, cust, days=8)
    out = send_reminder(
        invoice_id=inv.id, request=_request(),
        payload=SendReminderIn(channel="call", stage="friendly"),
        user=_user(), db=db,
    )
    assert out["sent"] is False
    assert outbox == []


# --------------------------------------------------------------------------
# The qualifier + automated task
# --------------------------------------------------------------------------


def test_auto_dunning_default_off(tenant_db_session, outbox):
    db = tenant_db_session
    cust = _seed_customer(db)
    _seed_overdue(db, cust, days=20)

    result = _run_auto_sends(db, TENANT)

    assert result["mode"] == "disabled"
    assert result["sent"] == 0
    assert outbox == []


def test_auto_dunning_sends_once_per_threshold(tenant_db_session, outbox):
    db = tenant_db_session
    cust = _seed_customer(db)
    inv = _seed_overdue(db, cust, days=8)  # crossed the 7-day threshold
    settings = _get_or_create_settings(db, TENANT)
    settings.auto_send_enabled = True
    db.commit()

    first = _run_auto_sends(db, TENANT)
    assert first["sent"] == 1
    row = db.execute(select(PaymentReminder)).scalars().one()
    assert row.threshold_days == 7
    assert row.sent_by == "auto-dunning"

    # Same tick again → idempotent, nothing re-fires.
    second = _run_auto_sends(db, TENANT)
    assert second["sent"] == 0
    assert len(outbox) == 1
    assert inv is not None


def test_auto_dunning_sends_highest_crossed_threshold_only(tenant_db_session, outbox):
    """Enable-day catch-up: a 40-day-overdue invoice gets ONE email (the
    30-day stage), not three."""
    db = tenant_db_session
    cust = _seed_customer(db)
    _seed_overdue(db, cust, days=40)
    settings = _get_or_create_settings(db, TENANT)
    settings.auto_send_enabled = True
    db.commit()

    result = _run_auto_sends(db, TENANT)

    assert result["sent"] == 1
    row = db.execute(select(PaymentReminder)).scalars().one()
    assert row.threshold_days == 30


def test_idempotency_survives_schedule_edit(tenant_db_session, outbox):
    db = tenant_db_session
    cust = _seed_customer(db)
    _seed_overdue(db, cust, days=12)
    settings = _get_or_create_settings(db, TENANT)
    settings.auto_send_enabled = True
    db.commit()

    _run_auto_sends(db, TENANT)  # fires threshold 7
    settings.schedule_days = "[5, 10]"
    db.commit()
    result = _run_auto_sends(db, TENANT)

    # Threshold 10 is new and crossed → fires once; 5 < 10 skipped by the
    # highest-crossed rule; the old 7 row doesn't wrongly suppress.
    assert result["sent"] == 1
    thresholds = {
        r.threshold_days for r in db.execute(select(PaymentReminder)).scalars().all()
    }
    assert thresholds == {7, 10}


def test_manual_log_never_suppresses_robot(tenant_db_session, outbox):
    db = tenant_db_session
    cust = _seed_customer(db)
    inv = _seed_overdue(db, cust, days=8)
    settings = _get_or_create_settings(db, TENANT)
    settings.auto_send_enabled = True
    db.commit()
    # Office logged a manual call yesterday (threshold_days NULL).
    db.add(PaymentReminder(
        invoice_id=inv.id, stage="friendly", channel="call",
        sent_at=datetime.now(UTC), sent_by="office-1",
    ))
    db.commit()

    result = _run_auto_sends(db, TENANT)
    assert result["sent"] == 1, "manual logs live in a separate keyspace"


def test_dunning_paused_mutes_invoice(tenant_db_session, outbox):
    db = tenant_db_session
    cust = _seed_customer(db)
    inv = _seed_overdue(db, cust, days=8)
    settings = _get_or_create_settings(db, TENANT)
    settings.auto_send_enabled = True
    db.commit()

    set_dunning_pause(
        invoice_id=inv.id, payload=DunningPauseIn(paused=True),
        request=_request(), user=_user(), db=db,
    )
    result = _run_auto_sends(db, TENANT)
    assert result["sent"] == 0
    assert outbox == []

    set_dunning_pause(
        invoice_id=inv.id, payload=DunningPauseIn(paused=False),
        request=_request(), user=_user(), db=db,
    )
    assert _run_auto_sends(db, TENANT)["sent"] == 1


def test_preview_matches_task_qualifier(tenant_db_session):
    """The settings-screen preview and the task must share ONE qualifier —
    the operator sees exactly who the first run emails."""
    db = tenant_db_session
    cust = _seed_customer(db)
    inv = _seed_overdue(db, cust, days=8)
    _seed_overdue(db, cust, days=2)   # overdue but under every threshold
    _seed_overdue(db, cust, days=9, status="draft")  # not a receivable

    settings = _get_or_create_settings(db, TENANT)
    preview = auto_send_preview(request=_request(), _=_user(), db=db)
    due = compute_due_sends(db, settings)

    assert preview["count"] == len(due) == 1
    assert preview["invoices"][0]["invoice_id"] == str(inv.id)


# --------------------------------------------------------------------------
# The weekly off-nudge
# --------------------------------------------------------------------------


def test_weekly_nudge_monday_only_and_permanent_dismiss(tenant_db_session, monkeypatch):
    db = tenant_db_session
    cust = _seed_customer(db)
    _seed_overdue(db, cust, days=20, balance=750.0)
    settings = _get_or_create_settings(db, TENANT)

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 6, 13, 0, tzinfo=UTC)  # a Monday

    monkeypatch.setattr("gdx_dispatch.tasks.invoice_reminders_auto.datetime", _FakeDT)
    assert _weekly_nudge(db, TENANT, settings) is True
    action = db.execute(select(NextAction)).scalars().one()
    assert action.action_type == "dunning_disabled_nudge"
    assert "750.00" in action.description

    # Tuesday → no nudge.
    class _FakeTue(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 7, 13, 0, tzinfo=UTC)

    monkeypatch.setattr("gdx_dispatch.tasks.invoice_reminders_auto.datetime", _FakeTue)
    assert _weekly_nudge(db, TENANT, settings) is False

    # Permanent dismiss suppresses even on Mondays.
    settings.auto_send_nudge_dismissed = True
    db.commit()
    monkeypatch.setattr("gdx_dispatch.tasks.invoice_reminders_auto.datetime", _FakeDT)
    assert _weekly_nudge(db, TENANT, settings) is False


def test_beat_and_includes_wired():
    """The dead-task failure mode must not repeat."""
    import inspect

    from gdx_dispatch.core import celery_app as celery_module
    from gdx_dispatch.core.scheduler import build_beat_schedule

    schedule = build_beat_schedule()
    assert schedule["invoice-auto-dunning-daily"]["task"] == "invoice_reminders.auto_dunning_tick"
    src = inspect.getsource(celery_module)
    assert "gdx_dispatch.tasks.invoice_reminders_auto" in src


def test_dunning_pause_serialized_on_invoice(tenant_db_session):
    from gdx_dispatch.routers.invoices import _serialize_invoice
    db = tenant_db_session
    cust = _seed_customer(db)
    inv = _seed_overdue(db, cust, days=8)
    assert _serialize_invoice(inv)["dunning_paused"] is False
    inv.dunning_paused = True
    db.commit()
    assert _serialize_invoice(inv)["dunning_paused"] is True


def test_catch_up_never_descends_day_by_day_walk(tenant_db_session, outbox):
    """Audit round 2 walk-through: enable auto-send on a 40-day-overdue
    invoice → ONE email (final stage, t=30) — and the NEXT days must be
    SILENT. Pre-fix, day 41 fired t=14 and day 42 fired t=7: three emails
    on three consecutive days in DESCENDING severity."""
    db = tenant_db_session
    cust = _seed_customer(db)
    _seed_overdue(db, cust, days=40)
    settings = _get_or_create_settings(db, TENANT)
    settings.auto_send_enabled = True
    db.commit()

    day1 = _run_auto_sends(db, TENANT)   # enable day
    day2 = _run_auto_sends(db, TENANT)   # next daily tick
    day3 = _run_auto_sends(db, TENANT)

    assert day1["sent"] == 1
    assert day2["sent"] == 0, "the escalation floor must silence lower stages"
    assert day3["sent"] == 0
    rows = db.execute(select(PaymentReminder)).scalars().all()
    assert [r.threshold_days for r in rows] == [30]
    assert len(outbox) == 1


def test_dunning_pause_is_office_only(tenant_db_session):
    """Pausing collections is a money decision — techs can't mute invoices."""
    from fastapi import HTTPException as _HTTPExc

    db = tenant_db_session
    cust = _seed_customer(db)
    inv = _seed_overdue(db, cust, days=8)

    with pytest.raises(_HTTPExc) as exc_info:
        set_dunning_pause(
            invoice_id=inv.id, payload=DunningPauseIn(paused=True),
            request=_request(),
            user={"user_id": "tech-1", "tenant_id": TENANT, "role": "technician"},
            db=db,
        )
    assert exc_info.value.status_code == 403
    db.refresh(inv)
    assert inv.dunning_paused is False


def test_skipped_send_does_not_consume_threshold(tenant_db_session, monkeypatch):
    """Audit round 2: with threshold_days recorded on a FAILED send, an SMTP
    outage would permanently eat that dunning stage via the escalation
    floor. Skips must retry on later ticks and deliver once fixed."""
    db = tenant_db_session
    cust = _seed_customer(db, email=None)  # no email yet → skip
    _seed_overdue(db, cust, days=8)
    settings = _get_or_create_settings(db, TENANT)
    settings.auto_send_enabled = True
    db.commit()

    first = _run_auto_sends(db, TENANT)
    assert first["skipped"] == 1 and first["sent"] == 0
    skip_row = db.execute(select(PaymentReminder)).scalars().one()
    assert skip_row.threshold_days is None, "a skip must not consume the stage"
    assert "(t=7)" in skip_row.notes

    # Office fixes the customer email → the next tick delivers t=7.
    cust.email = "amy@example.com"
    db.commit()

    sent_calls = []
    monkeypatch.setattr(
        "gdx_dispatch.core.transactional_email.send_transactional_email",
        lambda **kw: (sent_calls.append(kw) or (True, "smtp", None)),
    )
    second = _run_auto_sends(db, TENANT)
    assert second["sent"] == 1
    delivered = [
        r for r in db.execute(select(PaymentReminder)).scalars().all()
        if r.threshold_days == 7
    ]
    assert len(delivered) == 1


def test_completed_nudge_stays_gone_for_the_week(tenant_db_session, monkeypatch):
    """Audit round 2 zombie fix: completing the Monday nudge must not
    resurrect it the same week."""
    db = tenant_db_session
    cust = _seed_customer(db)
    _seed_overdue(db, cust, days=20)
    settings = _get_or_create_settings(db, TENANT)

    class _Mon(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 6, 13, 0, tzinfo=UTC)

    monkeypatch.setattr("gdx_dispatch.tasks.invoice_reminders_auto.datetime", _Mon)
    assert _weekly_nudge(db, TENANT, settings) is True
    action = db.execute(select(NextAction)).scalars().one()
    action.status = "completed"
    action.completed_at = datetime.now(UTC)
    db.commit()

    # Same Monday, later tick (or a retry) — no zombie.
    assert _weekly_nudge(db, TENANT, settings) is False
    assert len(db.execute(select(NextAction)).scalars().all()) == 1
