"""The on-my-way text (core/on_my_way_sms.py, GDXA-1201).

The helper queues the text and the real scheduled-text drain sends it. Here
the real drain and PhoneComClient run with only the HTTP call intercepted
(respx), so "sent" means a POST to Phone.com with this body went out. The
opt-in switch is driven through its real route with TestClient.

What they defend, worst first:

  1. Nothing is texted unless the tenant opted in (the default is off), and
     never to a customer who opted out or has no textable number.
  2. ``queued`` is True only when a text was really queued — it is what the
     routes report as ``customer_notified``.
  3. A failure never escapes: the helper returns, it does not raise, and it
     never touches a write the caller already committed.
  4. A re-tap (or a second tech) inside the window does not text twice.
  5. Every send and every skip leaves an audit row.

The two en-route routes call this helper in their own changes (mobile-tech,
jobs-dispatch); their TestClient tests live with them.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import on_my_way_sms
from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.models.tenant_models import AppSettings, Customer, Job, Notification, NotificationTemplate, User
from gdx_dispatch.modules.phone_com import scheduled
from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComClient
from gdx_dispatch.modules.phone_com.models import PhoneComMessage, ScheduledSms

TENANT = str(uuid4())
VOIP = 4242
SEND_URL = f"{BASE_URL}/accounts/{VOIP}/messages"
FROM = "+16125550100"
CUSTOMER_E164 = "+16125550142"
TECH_ID = uuid4()
TECH = {"user_id": str(TECH_ID), "sub": str(TECH_ID), "tenant_id": TENANT, "role": "technician"}
ADMIN_ID = uuid4()
ADMIN = {"user_id": str(ADMIN_ID), "tenant_id": TENANT, "role": "admin"}


class _State:
    tenant = {"id": TENANT}


class _Req:
    state = _State()
    headers: dict = {}
    client = None


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://gdx.example.com")

    def _client(tenant_id, control_db, tenant_db):
        assert str(tenant_id) == TENANT
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
    for tbl in [Job.__table__, Customer.__table__]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    s = Session()
    s.add(AppSettings(company_name="Acme Doors", phone_com_default_caller_id=FROM, on_my_way_sms_enabled=True))
    s.add(User(id=TECH_ID, username="sam", full_name="Sam Rivera", role="technician", active=True, company_id=TENANT))
    s.add(User(id=ADMIN_ID, username="office", role="admin", active=True, company_id=TENANT))
    s.execute(TenantBase.metadata.tables["technicians"].insert().values(
        id="tech-sam", user_id=str(TECH_ID), company_id=TENANT, name="Sam",
    ))
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _job(db, *, opt_out=False, phone="(612) 555-0142") -> Job:
    c = Customer(id=uuid4(), name="Pat Payer", phone=phone, sms_opt_out=opt_out, company_id=TENANT)
    db.add(c)
    j = Job(id=uuid4(), customer_id=c.id, title="Spring", lifecycle_stage="scheduled",
            dispatch_status="en_route", billing_status="unbilled", company_id=TENANT)
    db.add(j)
    db.commit()
    return j


def _send(db, job, **kw):
    return on_my_way_sms.send_on_my_way(db, user=kw.pop("user", TECH), request=_Req(), job_id=job.id, **kw)


def _audit(db, action):
    return db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


def _drain(db):
    return scheduled.drain(db, now=datetime.now(UTC) + timedelta(seconds=5))


def _ok(route):
    return route.mock(return_value=httpx.Response(200, json={"id": "pc-1", "status": "sent"}))


# ── sent ─────────────────────────────────────────────────────────────────


@respx.mock
def test_opted_in_tenant_texts_the_customer_once_through_the_real_drain(db):
    route = _ok(respx.post(SEND_URL))
    job = _job(db)

    result = _send(db, job, eta_minutes=15)
    assert result.queued is True and result.reason is None

    _drain(db)
    assert route.call_count == 1
    sent = route.calls[0].request
    payload = json.loads(sent.content)
    assert payload["to"] == CUSTOMER_E164 and payload["from"] == FROM
    assert payload["text"] == "Hi Pat, Sam from Acme Doors is on the way and should be there in about 15 minutes."

    db.expire_all()
    row = db.get(ScheduledSms, UUID(result.scheduled_sms_id))
    assert row.status == "sent" and row.job_id == job.id and row.audit_action == "on_my_way_sms"
    msg = db.get(PhoneComMessage, row.phone_com_message_row_id)
    assert msg.direction == "out" and msg.job_id == job.id and msg.sent_by_user_id == TECH_ID

    [queued] = _audit(db, "on_my_way_sms_queued")
    assert queued.details["scheduled_sms_id"] == result.scheduled_sms_id
    assert queued.details["to_last4"] == "0142"
    assert _audit(db, "scheduled_sms_sent")


@respx.mock
def test_a_retap_inside_the_window_does_not_text_twice(db):
    route = _ok(respx.post(SEND_URL))
    job = _job(db)
    assert _send(db, job).queued is True
    _drain(db)

    again = _send(db, job)
    assert (again.queued, again.reason) == (False, "already_notified")
    _drain(db)
    assert route.call_count == 1
    assert [a.details["reason"] for a in _audit(db, "on_my_way_sms_skipped")] == ["already_notified"]


@respx.mock
def test_a_text_that_failed_does_not_block_the_next_tap(db):
    respx.post(SEND_URL).mock(return_value=httpx.Response(400, json={"error": "bad"}))
    job = _job(db)
    assert _send(db, job).queued is True
    _drain(db)
    assert _send(db, job).queued is True


def test_a_notification_template_row_never_changes_the_wording(db):
    # Any signed-in user can create an on_my_way template row, and no settings
    # screen shows one: the text must not read it.
    now = datetime.now(UTC).isoformat()
    for default in (1, 0):
        db.add(NotificationTemplate(id=str(uuid4()), tenant_id=TENANT, template_key="on_my_way", subject="x",
                                    body="Call 555-0000 to reschedule", is_default=default, created_at=now))
    db.commit()
    r = _send(db, _job(db))
    assert db.get(ScheduledSms, UUID(r.scheduled_sms_id)).body == "Hi Pat, Sam from Acme Doors is on the way."


# ── skipped ──────────────────────────────────────────────────────────────


def test_flag_off_queues_nothing_and_is_not_an_attempt(db):
    db.query(AppSettings).first().on_my_way_sms_enabled = False
    db.commit()
    r = _send(db, _job(db))
    assert (r.queued, r.reason) == (False, "disabled")
    assert db.execute(select(ScheduledSms)).first() is None
    assert _audit(db, "on_my_way_sms_skipped") == []


def test_a_fresh_install_reads_the_flag_off():
    assert AppSettings.__table__.c.on_my_way_sms_enabled.server_default.arg == "false"
    assert AppSettings().on_my_way_sms_enabled in (None, False)


@pytest.mark.parametrize(
    ("job_kw", "reason"),
    [({"opt_out": True}, "sms_opt_out"), ({"phone": None}, "no_valid_phone"), ({"phone": "12"}, "no_valid_phone")],
)
def test_opted_out_or_unreachable_customer_is_skipped_and_audited(db, job_kw, reason):
    job = _job(db, **job_kw)
    r = _send(db, job)
    assert (r.queued, r.reason) == (False, reason)
    assert db.execute(select(ScheduledSms)).first() is None
    [skip] = _audit(db, "on_my_way_sms_skipped")
    assert skip.details["reason"] == reason and skip.entity_id == str(job.id)


@respx.mock
def test_a_customer_who_opts_out_before_the_drain_is_not_texted(db):
    route = _ok(respx.post(SEND_URL))
    job = _job(db)
    assert _send(db, job).queued is True
    db.get(Customer, job.customer_id).sms_opt_out = True
    db.commit()
    _drain(db)
    assert route.call_count == 0
    assert db.execute(select(ScheduledSms.error_code)).scalar() == "sms_opt_out"


def test_texting_switched_off_is_skipped(db, monkeypatch):
    monkeypatch.setattr("gdx_dispatch.core.modules.is_module_enabled", lambda key, request, db: False)
    r = _send(db, _job(db))
    assert (r.queued, r.reason) == (False, "texting_disabled")


def test_no_outbound_number_is_skipped(db):
    db.query(AppSettings).first().phone_com_default_caller_id = None
    db.commit()
    r = _send(db, _job(db))
    assert (r.queued, r.reason) == (False, "no_outbound_number")


def test_no_signed_in_user_is_skipped(db):
    r = _send(db, _job(db), user={"tenant_id": TENANT, "user_id": "system"})
    assert (r.queued, r.reason) == (False, "no_user")


# ── failure never escapes ────────────────────────────────────────────────


def test_an_error_returns_not_queued_and_leaves_the_committed_write_alone(db, monkeypatch):
    job = _job(db)
    # The en-route write the route committed just before calling the helper.
    job.dispatch_status = "on_site"
    db.commit()

    def _boom(*a, **k):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr("gdx_dispatch.modules.phone_com.outbound_did.resolve_outbound_did", _boom)
    r = _send(db, job)
    assert (r.queued, r.reason) == (False, "error")
    db.expire_all()
    assert db.get(Job, job.id).dispatch_status == "on_site"
    assert db.execute(select(ScheduledSms)).first() is None
    [failed] = _audit(db, "on_my_way_sms_failed")
    assert failed.details["error"] == "RuntimeError"


def test_flushed_but_uncommitted_caller_work_is_neither_committed_nor_rolled_back(db):
    job = _job(db)
    job.title = "CALLER_UNCOMMITTED"
    db.flush()  # sent to the database, not committed: db.dirty is empty now
    assert not db.dirty
    r = _send(db, job)
    assert (r.queued, r.reason) == (False, "caller_uncommitted")
    assert job.title == "CALLER_UNCOMMITTED"  # not rolled back by the helper
    db.rollback()  # the caller's own choice: had the helper committed, this would not undo it
    db.expire_all()
    assert db.get(Job, job.id).title == "Spring"
    assert _audit(db, "on_my_way_sms_skipped") == []


@respx.mock
def test_switched_off_after_queueing_is_not_sent(db):
    route = _ok(respx.post(SEND_URL))
    r = _send(db, _job(db))
    assert r.queued is True
    db.query(AppSettings).first().on_my_way_sms_enabled = False
    db.commit()
    _drain(db)
    assert route.call_count == 0
    db.expire_all()
    row = db.get(ScheduledSms, UUID(r.scheduled_sms_id))
    assert (row.status, row.error_code) == ("skipped", "on_my_way_disabled")


def test_uncommitted_caller_changes_are_neither_committed_nor_rolled_back(db):
    job = _job(db)
    job.dispatch_status = "on_site"  # pending, not committed
    r = _send(db, job)
    assert (r.queued, r.reason) == (False, "caller_uncommitted")
    assert job in db.dirty
    assert db.execute(select(ScheduledSms)).first() is None


@respx.mock
def test_a_provider_timeout_is_unknown_never_resent_and_reaches_the_office_bell(db):
    route = respx.post(SEND_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    job = _job(db)
    r = _send(db, job)
    assert r.queued is True
    _drain(db)
    _drain(db)
    assert route.call_count == 1
    db.expire_all()
    assert db.get(ScheduledSms, UUID(r.scheduled_sms_id)).status == "unknown"
    [bell] = db.execute(select(Notification)).scalars().all()
    assert "on-my-way text" in bell.message


# ── the opt-in switch, through its route ─────────────────────────────────


def _settings_client(db, user):
    from gdx_dispatch.core.auth import get_current_user
    from gdx_dispatch.core.database import get_db, get_tenant_db
    from gdx_dispatch.core.tenant_settings import Base as ControlBase
    from gdx_dispatch.routers import phone_com_settings

    # The GET reads the token state from the control plane's tenant_settings.
    for name in ("tenants", "tenant_settings"):
        ControlBase.metadata.tables[name].create(db.get_bind(), checkfirst=True)
    app = FastAPI()
    app.include_router(phone_com_settings.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_tenant_db] = lambda: db
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_admin_turns_it_on_through_the_route_and_the_get_reads_it_back(db):
    db.query(AppSettings).first().on_my_way_sms_enabled = False
    db.commit()
    client = _settings_client(db, ADMIN)
    assert client.get("/api/settings/integrations/phone-com").json()["on_my_way_sms_enabled"] is False

    resp = client.put("/api/settings/integrations/phone-com/on-my-way", json={"enabled": True})
    assert resp.status_code == 200 and resp.json() == {"enabled": True}
    assert client.get("/api/settings/integrations/phone-com").json()["on_my_way_sms_enabled"] is True
    [a] = _audit(db, "phone_com.on_my_way_sms_set")
    assert a.details == {"from": False, "to": True} and a.user_id == str(ADMIN_ID)


def test_a_tech_cannot_flip_it(db):
    client = _settings_client(db, TECH)
    resp = client.put("/api/settings/integrations/phone-com/on-my-way", json={"enabled": False})
    assert resp.status_code == 403
    db.expire_all()
    assert db.query(AppSettings).first().on_my_way_sms_enabled is True
    assert _audit(db, "phone_com.on_my_way_sms_set") == []
