"""The office On My Way texts the customer (GDXA-1203).

``POST /api/appointments/{id}/on-my-way`` calls the shared helper
(core/on_my_way_sms.py) after the en-route write commits. These drive the
real route with TestClient over a real SQLite schema; for "sent" the real
scheduled-text drain and PhoneComClient run with only the HTTP call
intercepted (respx). Nothing in the route is mocked.

What they defend, worst first:

  1. A text that cannot go never fails, or undoes, the en-route write
     (it is load-bearing for time tracking).
  2. ``customer_notified`` is True only when a text was really queued.
  3. Nothing is texted unless the tenant opted in, and never to a customer
     who opted out.
  4. The send and every skip leave an audit row.
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

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.models.tenant_models import AppSettings, Customer, Job, Technician, User
from gdx_dispatch.modules.phone_com import scheduled
from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComClient
from gdx_dispatch.modules.phone_com.models import ScheduledSms
from gdx_dispatch.routers.appointments import Appointment, router
from gdx_dispatch.routers.auth import get_current_user

TENANT = str(uuid4())
VOIP = 4242
SEND_URL = f"{BASE_URL}/accounts/{VOIP}/messages"
FROM = "+16125550100"
OFFICE_ID = uuid4()
OFFICE = {"user_id": str(OFFICE_ID), "sub": str(OFFICE_ID), "tenant_id": TENANT, "role": "admin"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://gdx.example.com")

    def _client(tenant_id, control_db, tenant_db):
        assert str(tenant_id) == TENANT
        c = PhoneComClient(token="t", voip_id=VOIP)
        c._backoff_seconds = lambda attempt: 0
        return c

    monkeypatch.setattr("gdx_dispatch.modules.phone_com.router._get_phone_com_client", _client)
    # The module-grant lookup reads the control plane; the tenant has texting on.
    monkeypatch.setattr("gdx_dispatch.core.modules.is_module_enabled", lambda key, request, db: True)
    monkeypatch.setattr(
        "gdx_dispatch.core.webhooks.emit.emit_domain_event", lambda db, name, entity_id, payload, **kw: None
    )


@pytest.fixture
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = Session()
    s.add(AppSettings(company_name="Acme Doors", phone_com_default_caller_id=FROM, on_my_way_sms_enabled=True))
    s.add(User(id=OFFICE_ID, username="dana", full_name="Dana Office", role="admin", active=True, company_id=TENANT))
    s.add(Technician(id="tech-mike", user_id=str(uuid4()), company_id=TENANT, name="Mike Tech"))
    s.commit()

    def _db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def _tenant(request, call_next):
        request.state.tenant = {"id": TENANT}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: OFFICE
    try:
        yield TestClient(app), s
    finally:
        s.close()
        engine.dispose()


def _visit(db, *, opt_out=False, with_job=True, tech_id="tech-mike") -> Appointment:
    c = Customer(id=uuid4(), name="Pat Payer", phone="(612) 555-0142", sms_opt_out=opt_out, company_id=TENANT)
    db.add(c)
    job_id = None
    if with_job:
        j = Job(id=uuid4(), customer_id=c.id, title="Spring", lifecycle_stage="scheduled",
                dispatch_status="assigned", billing_status="unbilled", company_id=TENANT)
        db.add(j)
        job_id = j.id
    start = datetime.now(UTC) + timedelta(hours=1)
    a = Appointment(id=uuid4(), company_id=TENANT, job_id=job_id, customer_id=c.id, title="Spring", tech_id=tech_id,
                    start_at=start, end_at=start + timedelta(hours=1), status="confirmed")
    db.add(a)
    db.commit()
    return a


def _audit(db, action):
    db.expire_all()
    return db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


def _queued(db):
    db.expire_all()
    return db.execute(select(ScheduledSms)).scalars().all()


def _en_route_committed(db, a) -> bool:
    db.expire_all()
    row = db.get(Appointment, a.id)
    return row.status == "en_route" and row.en_route_at is not None


@respx.mock
def test_on_my_way_texts_the_customer_and_reports_it(env):
    client, db = env
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-1", "status": "sent"}))
    a = _visit(db)

    r = client.post(f"/api/appointments/{a.id}/on-my-way")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "en_route" and body["en_route_at"]
    assert body["customer_notified"] is True and body["customer_notified_reason"] is None
    assert _en_route_committed(db, a)

    rows = _queued(db)
    assert len(rows) == 1 and rows[0].job_id == a.job_id and rows[0].created_by_user_id == OFFICE_ID
    queued = _audit(db, "on_my_way_sms_queued")
    assert len(queued) == 1 and queued[0].user_id == str(OFFICE_ID)

    scheduled.drain(db, now=datetime.now(UTC) + timedelta(seconds=5))
    assert route.call_count == 1
    sent = json.loads(route.calls[0].request.content)
    assert sent["to"] == "+16125550142"
    # The office pressed it, but the customer is told who is actually coming.
    assert sent["text"] == "Hi Pat, Mike from Acme Doors is on the way."


    # A second press inside the window texts nothing more, and says so.
    r2 = client.post(f"/api/appointments/{a.id}/on-my-way")
    assert r2.status_code == 200
    assert r2.json()["customer_notified"] is False
    assert r2.json()["customer_notified_reason"] == "already_notified"


def test_visit_with_no_job_texts_its_customer(env):
    client, db = env
    a = _visit(db, with_job=False)

    r = client.post(f"/api/appointments/{a.id}/on-my-way")
    assert r.status_code == 200, r.text
    assert r.json()["customer_notified"] is True
    rows = _queued(db)
    assert len(rows) == 1 and rows[0].job_id is None and rows[0].customer_id == a.customer_id


def test_opted_out_customer_is_not_texted_and_the_skip_is_audited(env):
    client, db = env
    a = _visit(db, opt_out=True)

    r = client.post(f"/api/appointments/{a.id}/on-my-way")
    assert r.status_code == 200, r.text
    assert r.json()["customer_notified"] is False
    assert r.json()["customer_notified_reason"] == "sms_opt_out"
    assert _en_route_committed(db, a)
    assert _queued(db) == []
    skipped = _audit(db, "on_my_way_sms_skipped")
    assert len(skipped) == 1 and skipped[0].entity_id == str(a.job_id)
    assert skipped[0].details["reason"] == "sms_opt_out"


def test_flag_off_sends_nothing(env):
    client, db = env
    db.query(AppSettings).update({AppSettings.on_my_way_sms_enabled: False})
    db.commit()
    a = _visit(db)

    r = client.post(f"/api/appointments/{a.id}/on-my-way")
    assert r.status_code == 200, r.text
    assert r.json()["customer_notified"] is False
    assert r.json()["customer_notified_reason"] == "disabled"
    assert _en_route_committed(db, a)
    assert _queued(db) == []
    # The en-route itself is still audited; the opt-out of the tenant is not an attempt.
    assert len(_audit(db, "appointment_en_route")) == 1
    assert _audit(db, "on_my_way_sms_skipped") == []


def test_send_failure_keeps_the_en_route_write(env, monkeypatch):
    client, db = env
    a = _visit(db)

    def _boom(*args, **kwargs):
        raise RuntimeError("phone.com number lookup down")

    monkeypatch.setattr("gdx_dispatch.modules.phone_com.outbound_did.resolve_outbound_did", _boom)

    r = client.post(f"/api/appointments/{a.id}/on-my-way")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "en_route"
    assert r.json()["customer_notified"] is False
    assert r.json()["customer_notified_reason"] == "error"
    assert _en_route_committed(db, a)
    assert len(_audit(db, "appointment_en_route")) == 1
    assert _queued(db) == []
    failed = _audit(db, "on_my_way_sms_failed")
    assert len(failed) == 1 and failed[0].details["error"] == "RuntimeError"


def test_unassigned_visit_names_no_one_and_never_the_office_user(env):
    client, db = env
    a = _visit(db, tech_id=None)
    r = client.post(f"/api/appointments/{a.id}/on-my-way")
    assert r.json()["customer_notified"] is True
    row = _queued(db)[0]
    assert row.body == "Hi Pat, your technician from Acme Doors is on the way."
    # The office user is who answers for it: sender and audit actor, not the name in the text.
    assert UUID(str(row.created_by_user_id)) == OFFICE_ID
