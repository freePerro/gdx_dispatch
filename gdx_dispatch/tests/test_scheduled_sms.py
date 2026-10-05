"""Scheduled texts — "send later" for replies, invoice links and estimate links
(modules/phone_com/scheduled.py).

The Phone.com HTTP call is intercepted with respx, but the real PhoneComClient
builds and sends it, and the real adapters (core/invoice_sms.py,
core/estimate_sms.py) run — so these assert what actually goes over the wire.

What they defend, worst first:

  1. A scheduled text is sent at most once: two drains, a drain and a cancel,
     a failure followed by another drain — never a second POST.
  2. Nothing is sent before its time (SQLite drops offsets; stored UTC).
  3. It is judged when it sends, not when it was scheduled: an invoice paid
     down overnight is texted with the new balance, one paid off or an
     estimate accepted is not texted, a customer who opted out is not texted,
     and a scheduler who lost their account or permission sends nothing.
  4. A worker killed mid-send leaves "unknown", not "sending" forever.
  5. Every outcome leaves an audit row.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.modules import is_module_enabled as _real_is_module_enabled  # before the autouse stub
from gdx_dispatch.models.tenant_models import AppSettings, Customer, Invoice, InvoiceLine, Job, Payment, User
from gdx_dispatch.modules.phone_com import scheduled
from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComClient
from gdx_dispatch.modules.phone_com.models import PhoneComMessage, ScheduledSms
from gdx_dispatch.modules.phone_com.router import cancel_scheduled, list_scheduled, schedule_message
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.routers.estimates import schedule_estimate_sms
from gdx_dispatch.routers.invoices import schedule_invoice_sms
from gdx_dispatch.routers.mobile_invoicing import mobile_schedule_invoice_sms

TENANT = str(uuid4())
VOIP = 4242
SEND_URL = f"{BASE_URL}/accounts/{VOIP}/messages"
FROM = "+16125550100"
CUSTOMER_PHONE = "(612) 555-0142"
CUSTOMER_E164 = "+16125550142"
OFFICE_ID = uuid4()
OFFICE = {"user_id": str(OFFICE_ID), "tenant_id": TENANT, "role": "admin"}
TECH_ID = uuid4()
TECH = {"user_id": str(TECH_ID), "sub": str(TECH_ID), "tenant_id": TENANT, "role": "technician"}


class _State:
    tenant = {"id": TENANT}


class _Req:
    state = _State()
    headers: dict = {}
    client = None


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://gdx.example.com")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")

    def _client(tenant_id, control_db, tenant_db):
        assert str(tenant_id) == TENANT  # the id the Phone.com token is keyed by
        c = PhoneComClient(token="t", voip_id=VOIP)
        c._backoff_seconds = lambda attempt: 0
        return c

    monkeypatch.setattr("gdx_dispatch.modules.phone_com.router._get_phone_com_client", _client)
    monkeypatch.setattr("gdx_dispatch.core.modules.is_module_enabled", lambda key, request, db: True)
    monkeypatch.setattr(
        "gdx_dispatch.core.webhooks.emit.emit_domain_event", lambda db, name, entity_id, payload, **kw: None
    )


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [Job.__table__, Customer.__table__, Invoice.__table__, InvoiceLine.__table__,
                Payment.__table__, Estimate.__table__]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    s = Session()
    s.add(AppSettings(company_name="Acme Doors", phone_com_default_caller_id=FROM))
    s.add(User(id=OFFICE_ID, username="office", role="admin", active=True, company_id=TENANT))
    s.add(User(id=TECH_ID, username="tech", role="technician", active=True, company_id=TENANT))
    s.execute(TenantBase.metadata.tables["technicians"].insert().values(
        id="tech-mine", user_id=str(TECH_ID), company_id=TENANT, name="Mine",
    ))
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _customer(db, *, opt_out=False) -> Customer:
    c = Customer(id=uuid4(), name="Pat Payer", phone=CUSTOMER_PHONE, sms_opt_out=opt_out, company_id=TENANT)
    db.add(c)
    db.commit()
    return c


def _invoice(db, *, balance=250.0, status="sent", verified=True) -> Invoice:
    c = _customer(db)
    j = Job(id=uuid4(), customer_id=c.id, title="Spring", lifecycle_stage="completed",
            dispatch_status="assigned", billing_status="unbilled", assigned_to="tech-mine", company_id=TENANT)
    db.add(j)
    inv = Invoice(
        id=uuid4(), job_id=j.id, customer_id=c.id, invoice_number="INV-1001",
        billing_type="standard", sequence_number=1,
        subtotal=Decimal(str(balance)), tax_amount=0, total=Decimal(str(balance)),
        balance_due=Decimal(str(balance)), status=status,
        invoice_date=date.today(), due_date=date.today(), company_id=TENANT,
        verified_at=datetime.now(UTC) if verified else None, created_at=datetime.now(UTC),
        public_token=uuid4().hex,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def _estimate(db, *, status="draft") -> Estimate:
    c = _customer(db)
    est = Estimate(
        id=uuid4(), customer_id=c.id, estimate_number="E-77", label="Door", proposal_mode=False,
        total=Decimal("1850"), status=status, public_token=uuid4().hex, company_id=TENANT,
    )
    db.add(est)
    db.commit()
    db.refresh(est)
    return est


def _later(minutes=60) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


def _schedule_invoice(db, inv, *, send_at=None, **payload):
    return schedule_invoice_sms(
        invoice_id=inv.id, request=_Req(),
        payload=scheduled.ScheduleLinkSmsIn(send_at=send_at or _later(), **payload), _=OFFICE, db=db,
    )


def _drain_at(db, when: datetime):
    return scheduled.drain(db, now=when)


def _row(db, row_id) -> ScheduledSms:
    db.expire_all()
    return db.get(ScheduledSms, UUID(row_id))


def _audit(db, action):
    return db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


def _ok(route, msg_id="pc-1"):
    return route.mock(return_value=httpx.Response(200, json={"id": msg_id, "status": "sent"}))


# ── scheduling ────────────────────────────────────────────────────────────


def test_schedule_invoice_stores_utc_and_no_body_for_the_default_wording(db):
    inv = _invoice(db)
    local = datetime.now(UTC).astimezone(timezone(timedelta(hours=-5))) + timedelta(hours=2)
    out = _schedule_invoice(db, inv, send_at=local)

    row = _row(db, out["id"])
    assert row.status == "scheduled" and row.kind == "invoice" and row.entity_id == inv.id
    assert row.body is None  # rebuilt at send time
    assert row.to_number == CUSTOMER_E164 and row.created_by_user_id == OFFICE_ID
    assert scheduled._utc(row.send_at) == local.astimezone(UTC)
    assert out["send_at"].endswith("Z")
    [a] = _audit(db, "sms_scheduled")
    assert a.entity_id == out["id"] and a.user_id == str(OFFICE_ID)


def test_schedule_keeps_an_edited_body(db):
    inv = _invoice(db)
    out = _schedule_invoice(db, inv, body="Thanks for today — here is your invoice.")
    assert _row(db, out["id"]).body == "Thanks for today — here is your invoice."


@pytest.mark.parametrize(
    ("send_at", "code"),
    [
        (datetime.now() + timedelta(hours=1), "send_at_no_timezone"),
        (datetime.now(UTC) + timedelta(seconds=10), "send_at_too_soon"),
        (datetime.now(UTC) - timedelta(hours=1), "send_at_too_soon"),
        (datetime.now(UTC) + timedelta(days=31), "send_at_too_far"),
    ],
)
def test_schedule_refuses_a_bad_send_time(db, send_at, code):
    inv = _invoice(db)
    with pytest.raises(HTTPException) as exc:
        _schedule_invoice(db, inv, send_at=send_at)
    assert exc.value.status_code == 422 and exc.value.detail["code"] == code
    assert db.execute(select(ScheduledSms)).first() is None


def test_schedule_refuses_now_what_send_would_refuse(db):
    inv = _invoice(db, status="void")
    with pytest.raises(HTTPException) as exc:
        _schedule_invoice(db, inv)
    assert exc.value.detail["code"] == "invoice_void"
    assert db.execute(select(ScheduledSms)).first() is None


@respx.mock
def test_unedited_default_posted_back_by_the_dialog_is_still_rebuilt(db):
    """The dialog fills its text box from the preview and posts it back
    unedited — that must still be stored as "default", not frozen."""
    from gdx_dispatch.core import invoice_sms

    route = _ok(respx.post(SEND_URL))
    inv = _invoice(db, balance=250)
    preview = invoice_sms.prepare(db, inv)
    out = _schedule_invoice(db, inv, body=preview["body"])
    assert _row(db, out["id"]).body is None
    inv.balance_due = Decimal("40.00")
    db.commit()

    _drain_at(db, _later(61))
    assert "$40.00 due" in json.loads(route.calls.last.request.content)["text"]


def test_claim_is_won_once_and_never_over_a_cancel(db):
    inv = _invoice(db)
    a = UUID(_schedule_invoice(db, inv)["id"])
    b = UUID(_schedule_invoice(db, inv, body="other")["id"])
    now = datetime.now(UTC)

    assert scheduled.claim(db, a, now=now) is True
    assert scheduled.claim(db, a, now=now) is False  # a second drain with the same due list

    cancel_scheduled(scheduled_id=b, request=_Req(), tenant_db=db, user=OFFICE)
    assert scheduled.claim(db, b, now=now) is False  # the cancel landed first
    assert _row(db, str(b)).status == "canceled"


# ── the drain: timing and once-only ──────────────────────────────────────


@respx.mock
def test_nothing_sends_before_its_time_and_it_sends_once_after(db):
    route = _ok(respx.post(SEND_URL))
    inv = _invoice(db)
    # 8:00 AM at UTC-5 — the case SQLite would get wrong if the offset were stored.
    at = (datetime.now(UTC) + timedelta(hours=3)).astimezone(timezone(timedelta(hours=-5)))
    out = _schedule_invoice(db, inv, send_at=at)

    assert _drain_at(db, at.astimezone(UTC) - timedelta(minutes=1))["claimed"] == 0
    assert route.call_count == 0

    counts = _drain_at(db, at.astimezone(UTC) + timedelta(seconds=30))
    assert counts == {"reaped": 0, "claimed": 1, "sent": 1, "not_sent": 0}
    assert _drain_at(db, at.astimezone(UTC) + timedelta(minutes=5))["claimed"] == 0
    assert route.call_count == 1

    row = _row(db, out["id"])
    assert row.status == "sent" and row.sent_at is not None
    msg = db.get(PhoneComMessage, row.phone_com_message_row_id)
    assert msg.phone_com_message_id == "pc-1" and msg.sent_by_user_id == OFFICE_ID
    db.refresh(inv)
    assert inv.sent_via == "sms"
    assert len(_audit(db, "invoice_sent_sms_scheduled")) == 1
    assert len(_audit(db, "scheduled_sms_sent")) == 1


@respx.mock
def test_default_wording_is_rebuilt_with_the_balance_at_send_time(db):
    route = _ok(respx.post(SEND_URL))
    inv = _invoice(db, balance=250)
    _schedule_invoice(db, inv)
    inv.balance_due = Decimal("100.00")  # a partial payment overnight
    db.commit()

    _drain_at(db, _later(61))
    text = json.loads(route.calls.last.request.content)["text"]
    assert "$100.00 due" in text and "$250.00" not in text


@respx.mock
def test_paid_off_overnight_is_skipped_not_texted(db):
    route = _ok(respx.post(SEND_URL))
    inv = _invoice(db)
    out = _schedule_invoice(db, inv)
    inv.balance_due = Decimal("0")
    db.commit()

    _drain_at(db, _later(61))
    row = _row(db, out["id"])
    assert route.call_count == 0
    assert (row.status, row.error_code) == ("skipped", "no_balance_due")
    assert len(_audit(db, "scheduled_sms_skipped")) == 1
    # Nobody is watching the thread at 8 AM: the office bell says so.
    from gdx_dispatch.models.tenant_models import Notification

    [bell] = db.execute(select(Notification)).scalars().all()
    # Routed to billing, which a tech can open too; named by invoice number.
    assert bell.user_id is None and bell.category == "invoice"
    assert bell.title == "Scheduled text not sent"
    assert "Pat Payer" in bell.message and "INV-1001" in bell.message


@respx.mock
def test_canceled_text_never_sends_and_cannot_be_canceled_once_sending(db):
    route = _ok(respx.post(SEND_URL))
    inv = _invoice(db)
    first = _schedule_invoice(db, inv)
    second = _schedule_invoice(db, inv, body="Second one")

    out = cancel_scheduled(scheduled_id=UUID(first["id"]), request=_Req(), tenant_db=db, user=OFFICE)
    assert out["status"] == "canceled"
    assert len(_audit(db, "sms_schedule_canceled")) == 1

    # A row a drain has claimed cannot be canceled out from under it.
    row = _row(db, second["id"])
    row.status = "sending"
    row.attempted_at = datetime.now(UTC)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        cancel_scheduled(scheduled_id=row.id, request=_Req(), tenant_db=db, user=OFFICE)
    assert exc.value.status_code == 409 and exc.value.detail["code"] == "not_cancelable"

    _drain_at(db, _later(61))
    assert route.call_count == 0
    assert _row(db, first["id"]).status == "canceled"
    # An hour on, the claimed row is stale: reaped to "unknown", never resent.
    assert _row(db, second["id"]).status == "unknown"


@respx.mock
def test_stale_sending_row_becomes_unknown_and_is_never_resent(db):
    route = _ok(respx.post(SEND_URL))
    inv = _invoice(db)
    out = _schedule_invoice(db, inv)
    row = _row(db, out["id"])
    row.status = "sending"
    row.attempted_at = datetime.now(UTC) - timedelta(minutes=11)
    db.commit()

    counts = _drain_at(db, datetime.now(UTC) + timedelta(hours=2))
    row = _row(db, out["id"])
    assert counts["reaped"] == 1 and route.call_count == 0
    assert (row.status, row.error_code) == ("unknown", "worker_interrupted")
    assert len(_audit(db, "scheduled_sms_unknown")) == 1
    from gdx_dispatch.models.tenant_models import Notification

    [bell] = db.execute(select(Notification)).scalars().all()
    assert bell.title == "Scheduled text not confirmed" and bell.category == "invoice"


@respx.mock
@pytest.mark.parametrize(
    ("response", "status", "code"),
    [
        (httpx.Response(400, json={"error": "bad"}), "failed", "sms_provider_error"),
        (httpx.Response(503), "unknown", "sms_outcome_unknown"),
        (httpx.ReadTimeout("slow"), "unknown", "sms_outcome_unknown"),
    ],
)
def test_provider_outcomes_are_recorded_and_never_retried(db, response, status, code):
    route = respx.post(SEND_URL)
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    inv = _invoice(db)
    out = _schedule_invoice(db, inv)

    _drain_at(db, _later(61))
    _drain_at(db, _later(120))
    row = _row(db, out["id"])
    assert (row.status, row.error_code) == (status, code)
    assert route.call_count == 1
    from gdx_dispatch.models.tenant_models import Notification

    titles = [n.title for n in db.execute(select(Notification)).scalars().all()]
    assert titles == ["Scheduled text not confirmed" if status == "unknown" else "Scheduled text not sent"]


# ── the drain: the scheduler is re-checked ───────────────────────────────


@respx.mock
def test_deactivated_scheduler_sends_nothing(db):
    route = _ok(respx.post(SEND_URL))
    inv = _invoice(db)
    out = _schedule_invoice(db, inv)
    db.get(User, OFFICE_ID).active = False
    db.commit()

    _drain_at(db, _later(61))
    row = _row(db, out["id"])
    assert route.call_count == 0
    assert (row.status, row.error_code) == ("skipped", "scheduler_not_permitted")


@respx.mock
def test_scheduler_demoted_below_invoices_send_sends_nothing(db):
    route = _ok(respx.post(SEND_URL))
    inv = _invoice(db)
    out = _schedule_invoice(db, inv)
    db.get(User, OFFICE_ID).role = "technician"
    db.commit()

    _drain_at(db, _later(61))
    assert route.call_count == 0
    assert _row(db, out["id"]).error_code == "scheduler_not_permitted"


@respx.mock
def test_texting_switched_off_before_it_is_due_sends_nothing(db, monkeypatch):
    route = _ok(respx.post(SEND_URL))
    inv = _invoice(db)
    out = _schedule_invoice(db, inv)
    monkeypatch.setattr("gdx_dispatch.core.modules.is_module_enabled", lambda key, request, db: False)

    _drain_at(db, _later(61))
    assert route.call_count == 0
    assert _row(db, out["id"]).error_code == "texting_disabled"


@respx.mock
def test_the_real_module_check_runs_in_the_worker_and_honors_a_disable(db, monkeypatch):
    """No stub: the worker's tenant-only request must resolve the real grant."""
    from sqlalchemy import text as _text

    route = _ok(respx.post(SEND_URL))
    monkeypatch.setattr("gdx_dispatch.core.modules.is_module_enabled", _real_is_module_enabled)
    inv = _invoice(db)
    on = _schedule_invoice(db, inv)
    off = _schedule_invoice(db, inv, body="second")
    _drain_at(db, _later(61))  # first drain seeds the default grants; phone_com is on
    assert _row(db, on["id"]).status == "sent" and route.call_count == 1
    # The second row (same link, same number, same minute) was held as a
    # duplicate in that drain; make it due again and switch texting off.
    row = _row(db, off["id"])
    row.status, row.error_code, row.send_at = "scheduled", None, datetime.now(UTC)
    db.execute(_text("DELETE FROM company_module_grants WHERE module_key = 'phone_com'"))
    db.commit()

    _drain_at(db, _later(62))
    assert route.call_count == 1  # nothing new went out
    assert _row(db, off["id"]).error_code == "texting_disabled"


@respx.mock
def test_a_text_found_long_after_its_time_is_held_not_sent_late(db, monkeypatch):
    """The worker was down overnight: the 8 AM text must not go at 2 AM, or
    three days later, when it comes back."""
    route = _ok(respx.post(SEND_URL))
    inv = _invoice(db)
    out = _schedule_invoice(db, inv)
    row = _row(db, out["id"])
    row.send_at = datetime.now(UTC) - timedelta(minutes=31)
    db.commit()

    _drain_at(db, datetime.now(UTC))
    row = _row(db, out["id"])
    assert route.call_count == 0
    assert (row.status, row.error_code) == ("skipped", "too_late")
    assert len(_audit(db, "scheduled_sms_skipped")) == 1


@respx.mock
def test_a_text_a_few_minutes_late_still_goes(db):
    route = _ok(respx.post(SEND_URL))
    inv = _invoice(db)
    out = _schedule_invoice(db, inv)
    row = _row(db, out["id"])
    row.send_at = datetime.now(UTC) - timedelta(minutes=5)
    db.commit()

    _drain_at(db, datetime.now(UTC))
    assert route.call_count == 1 and _row(db, out["id"]).status == "sent"


def test_a_drain_past_its_budget_leaves_the_rest_for_the_next_minute(db, monkeypatch):
    inv = _invoice(db)
    ids = [_schedule_invoice(db, inv, body=f"t{i}")["id"] for i in range(2)]
    monkeypatch.setattr(scheduled, "DRAIN_BUDGET", timedelta(seconds=-1))  # already spent

    counts = _drain_at(db, _later(61))
    assert counts["claimed"] == 0
    assert [_row(db, i).status for i in ids] == ["scheduled", "scheduled"]


def test_a_late_claim_is_stamped_when_it_is_taken_not_when_the_drain_began(db):
    """A row claimed minutes into a slow batch must not look ten minutes old
    to the next drain's reaper while it is still being sent."""
    inv = _invoice(db)
    row_id = UUID(_schedule_invoice(db, inv)["id"])
    before = datetime.now(UTC)
    assert scheduled.claim(db, row_id) is True
    row = _row(db, str(row_id))
    assert scheduled._utc(row.attempted_at) >= before
    # The reaper, eleven minutes after the drain began but moments after
    # this claim, leaves it alone.
    assert scheduled.reap_stale(db, now=before + timedelta(minutes=1)) == 0
    assert _row(db, str(row_id)).status == "sending"


# ── estimates and the tech path ──────────────────────────────────────────


@respx.mock
def test_estimate_accepted_overnight_is_not_texted(db):
    route = _ok(respx.post(SEND_URL))
    est = _estimate(db)
    out = schedule_estimate_sms(
        estimate_id=est.id, request=_Req(), payload=scheduled.ScheduleLinkSmsIn(send_at=_later()), _=OFFICE, db=db,
    )
    est.status = "accepted"
    db.commit()

    _drain_at(db, _later(61))
    assert route.call_count == 0
    assert _row(db, out["id"]).error_code == "estimate_finalized"


@respx.mock
def test_estimate_sends_and_goes_live(db):
    route = _ok(respx.post(SEND_URL))
    est = _estimate(db)
    out = schedule_estimate_sms(
        estimate_id=est.id, request=_Req(), payload=scheduled.ScheduleLinkSmsIn(send_at=_later()), _=OFFICE, db=db,
    )
    _drain_at(db, _later(61))
    db.refresh(est)
    assert route.call_count == 1 and _row(db, out["id"]).status == "sent"
    assert est.status == "sent" and est.sent_via == "sms" and est.sent_at is not None
    assert f"/proposals/{est.public_token}" in json.loads(route.calls.last.request.content)["text"]


@respx.mock
def test_tech_scheduled_invoice_rechecks_office_verification(db):
    route = _ok(respx.post(SEND_URL))
    inv = _invoice(db)
    resp = mobile_schedule_invoice_sms(
        invoice_id=str(inv.id), request=_Req(),
        payload=scheduled.ScheduleLinkSmsIn(send_at=_later()), current_user=TECH, db=db,
    )
    assert resp.status_code == 201
    row_id = json.loads(resp.body)["id"]
    inv.verified_at = None  # the office pulled it back for review
    db.commit()

    _drain_at(db, _later(61))
    assert route.call_count == 0
    assert _row(db, row_id).error_code == "awaiting_verification"


@respx.mock
def test_tech_scheduled_invoice_is_not_sent_once_the_job_is_reassigned(db):
    route = _ok(respx.post(SEND_URL))
    inv = _invoice(db)
    resp = mobile_schedule_invoice_sms(
        invoice_id=str(inv.id), request=_Req(),
        payload=scheduled.ScheduleLinkSmsIn(send_at=_later()), current_user=TECH, db=db,
    )
    row_id = json.loads(resp.body)["id"]
    job = db.get(Job, inv.job_id)
    job.assigned_to = "someone-else"
    db.commit()

    _drain_at(db, _later(61))
    assert route.call_count == 0
    assert _row(db, row_id).error_code == "job_not_yours"


@respx.mock
def test_tech_scheduled_invoice_sends_while_the_job_is_still_theirs(db):
    route = _ok(respx.post(SEND_URL))
    inv = _invoice(db)
    resp = mobile_schedule_invoice_sms(
        invoice_id=str(inv.id), request=_Req(),
        payload=scheduled.ScheduleLinkSmsIn(send_at=_later()), current_user=TECH, db=db,
    )
    row_id = json.loads(resp.body)["id"]
    _drain_at(db, _later(61))
    assert route.call_count == 1 and _row(db, row_id).status == "sent"
    assert len(_audit(db, "mobile_invoice_sent_sms_scheduled")) == 1


# ── plain replies ────────────────────────────────────────────────────────


def _schedule_reply(db, *, customer=None, body="Sure, we can come Tuesday.", send_at=None):
    return schedule_message(
        payload=scheduled.ScheduleMessageIn(
            to="952-555-0199" if customer is None else CUSTOMER_PHONE, body=body,
            customer_id=customer.id if customer else None, send_at=send_at or _later(),
        ),
        request=_Req(), tenant_db=db, user=OFFICE,
    )


@respx.mock
def test_scheduled_reply_sends_and_lands_on_the_thread(db):
    route = _ok(respx.post(SEND_URL), msg_id="pc-reply")
    out = _schedule_reply(db)
    assert out["to_number"] == "+19525550199" and out["body"] == "Sure, we can come Tuesday."

    listed = list_scheduled(to="+19525550199", kind=None, entity_id=None, tenant_db=db, user=OFFICE)
    assert [i["id"] for i in listed["items"]] == [out["id"]]

    _drain_at(db, _later(61))
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"from": FROM, "to": "+19525550199", "text": "Sure, we can come Tuesday."}
    row = _row(db, out["id"])
    msg = db.get(PhoneComMessage, row.phone_com_message_row_id)
    assert row.status == "sent"
    assert (msg.phone_com_message_id, msg.direction, msg.sent_by_user_id) == ("pc-reply", "out", OFFICE_ID)
    assert len(_audit(db, "scheduled_sms_sent")) == 1
    # Sent texts show as real messages; the pending list no longer carries it.
    assert list_scheduled(to="+19525550199", kind=None, entity_id=None, tenant_db=db, user=OFFICE)["items"] == []


@respx.mock
def test_reply_to_a_customer_who_opted_out_overnight_is_skipped(db):
    route = _ok(respx.post(SEND_URL))
    c = _customer(db)
    out = _schedule_reply(db, customer=c)
    c.sms_opt_out = True
    db.commit()

    _drain_at(db, _later(61))
    assert route.call_count == 0
    assert _row(db, out["id"]).error_code == "sms_opt_out"
    from gdx_dispatch.models.tenant_models import Notification

    [bell] = db.execute(select(Notification)).scalars().all()
    assert bell.category == "sms"  # a reply's alert opens the SMS page
    # A text that did not go stays visible beside the thread.
    [item] = list_scheduled(to=CUSTOMER_PHONE, kind=None, entity_id=None, tenant_db=db, user=OFFICE)["items"]
    assert item["status"] == "skipped"


def test_reply_to_an_opted_out_customer_is_refused_at_schedule_time(db):
    c = _customer(db, opt_out=True)
    with pytest.raises(HTTPException) as exc:
        _schedule_reply(db, customer=c)
    assert exc.value.detail["code"] == "sms_opt_out"


@respx.mock
def test_reply_timeout_is_unknown_and_the_pending_message_row_says_so(db):
    respx.post(SEND_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    out = _schedule_reply(db)
    _drain_at(db, _later(61))
    row = _row(db, out["id"])
    msg = db.get(PhoneComMessage, row.phone_com_message_row_id)
    assert row.status == "unknown" and msg.delivery_status == "unknown"


def test_a_blank_reply_cannot_be_scheduled(db):
    with pytest.raises(HTTPException) as exc:
        _schedule_reply(db, body="   ")
    assert exc.value.status_code == 422 and exc.value.detail["code"] == "empty_body"
    assert db.execute(select(ScheduledSms)).first() is None


def test_list_needs_a_filter(db):
    with pytest.raises(HTTPException) as exc:
        list_scheduled(to=None, kind=None, entity_id=None, tenant_db=db, user=OFFICE)
    assert exc.value.status_code == 422
