"""The tech's "On my way" tap texts the customer (GDXA-1202).

``POST /api/mobile/jobs/{id}/en-route`` calls the shared on-my-way helper
(core/on_my_way_sms.py) after the en-route write commits. These drive the real
route through TestClient; the helper, the scheduled-text queue and its drain
are real, and only the HTTP call to Phone.com is intercepted (respx).

What they defend, worst first:

  1. The en-route write lands whatever the text does — it is what time
     tracking reads. A send failure still answers 200 with the job en route.
  2. ``customer_notified`` is True only when this tap queued a text.
  3. Nothing is texted when the tenant has not opted in, or to a customer who
     opted out or has no textable number.
  4. Every send and every skip leaves an audit row.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import (
    Appointment,
    AppSettings,
    Customer,
    Job,
    Notification,
    Technician,
    User,
)
from gdx_dispatch.modules.phone_com import scheduled
from gdx_dispatch.modules.phone_com.client import BASE_URL, PhoneComClient
from gdx_dispatch.modules.phone_com.models import ScheduledSms
from gdx_dispatch.routers import mobile as mobile_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = str(uuid4())
USER_ID = uuid4()
TECH = "tech-sam"
VOIP = 4242
SEND_URL = f"{BASE_URL}/accounts/{VOIP}/messages"
FROM = "+16125550100"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
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
def app_and_db():
    engine = make_fresh_db()
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    s.add(AppSettings(company_name="Acme Doors", phone_com_default_caller_id=FROM, on_my_way_sms_enabled=True))
    s.add(User(id=USER_ID, username="sam", full_name="Sam Rivera", role="technician", active=True,
               company_id=TENANT))
    s.add(Technician(id=TECH, company_id=TENANT, user_id=str(USER_ID), active=True))
    s.commit()

    from gdx_dispatch.core.modules import require_module

    app = FastAPI()
    app.include_router(mobile_router.router)
    app.dependency_overrides[get_db] = lambda: s
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": str(USER_ID), "tenant_id": TENANT, "role": "technician",
    }
    app.dependency_overrides[require_module("mobile")] = lambda: True

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "test"}
        request.state.tenant_id = TENANT
        return await call_next(request)

    yield TestClient(app), s
    s.close()
    engine.dispose()


def _job(db, *, opt_out=False, phone="(612) 555-0142") -> Job:
    now = datetime.now(UTC)
    cust = Customer(id=uuid4(), name="Pat Payer", phone=phone, sms_opt_out=opt_out, company_id=TENANT)
    db.add(cust)
    job = Job(id=uuid4(), company_id=TENANT, customer_id=cust.id, title="Spring", description="",
              scheduled_at=now, assigned_to=TECH, dispatch_status="assigned")
    db.add(job)
    db.add(Appointment(id=uuid4(), company_id=TENANT, job_id=job.id, customer_id=cust.id, tech_id=TECH,
                       title="Today", start_at=now, end_at=now + timedelta(hours=1)))
    db.commit()
    return job


def _tap(client, job, eta=None):
    # SQLite stores the id as dashless hex and the route matches it raw; the
    # sibling mobile route tests address jobs the same way.
    # The phone posts {} (MobileJobDetailView onMyWay): no ETA.
    return client.post(f"/api/mobile/jobs/{job.id.hex}/en-route", json={} if eta is None else {"eta_minutes": eta})


def _en_route_committed(db, job) -> bool:
    db.expire_all()
    return db.get(Job, job.id).dispatch_status == "en_route"


def _audit(db, action):
    return db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


def _texts(db):
    return db.execute(select(ScheduledSms)).scalars().all()


# ── sent ─────────────────────────────────────────────────────────────────


@respx.mock
def test_tap_texts_the_customer_and_reports_it(app_and_db):
    client, db = app_and_db
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-1", "status": "sent"}))
    job = _job(db)

    resp = _tap(client, job)
    assert resp.status_code == 200, resp.text
    assert resp.json()["customer_notified"] is True
    assert resp.json()["dispatch_status"] == "en_route"
    assert _en_route_committed(db, job)

    [row] = _texts(db)
    assert row.job_id == job.id and row.audit_action == "on_my_way_sms"
    [queued] = _audit(db, "on_my_way_sms_queued")
    assert queued.entity_id == str(job.id) and queued.user_id == str(USER_ID)

    scheduled.drain(db, now=datetime.now(UTC) + timedelta(seconds=5))
    assert route.call_count == 1
    payload = json.loads(route.calls[0].request.content)
    assert payload["to"] == "+16125550142"
    assert payload["text"] == "Hi Pat, Sam from Acme Doors is on the way."


def test_a_retap_reports_false_and_queues_nothing_more(app_and_db):
    client, db = app_and_db
    job = _job(db)
    assert _tap(client, job).json()["customer_notified"] is True
    again = _tap(client, job)
    assert again.status_code == 200 and again.json()["customer_notified"] is False
    assert len(_texts(db)) == 1
    assert [a.details["reason"] for a in _audit(db, "on_my_way_sms_skipped")] == ["already_notified"]


# ── skipped ──────────────────────────────────────────────────────────────


def test_flag_off_moves_the_job_and_texts_nobody(app_and_db):
    client, db = app_and_db
    db.query(AppSettings).first().on_my_way_sms_enabled = False
    db.commit()
    job = _job(db)

    resp = _tap(client, job)
    assert resp.status_code == 200 and resp.json()["customer_notified"] is False
    assert _en_route_committed(db, job)
    assert _texts(db) == []


@pytest.mark.parametrize(
    ("job_kw", "reason"),
    [({"opt_out": True}, "sms_opt_out"), ({"phone": None}, "no_valid_phone")],
)
def test_opted_out_or_no_number_is_skipped_and_audited(app_and_db, job_kw, reason):
    client, db = app_and_db
    job = _job(db, **job_kw)

    resp = _tap(client, job)
    assert resp.status_code == 200 and resp.json()["customer_notified"] is False
    assert _en_route_committed(db, job)
    assert _texts(db) == []
    [skip] = _audit(db, "on_my_way_sms_skipped")
    assert skip.details["reason"] == reason and skip.entity_id == str(job.id)


# ── a failure never fails the tap ────────────────────────────────────────


def test_a_send_failure_still_puts_the_job_en_route(app_and_db, monkeypatch):
    client, db = app_and_db
    job = _job(db)

    def _boom(*a, **k):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr("gdx_dispatch.modules.phone_com.outbound_did.resolve_outbound_did", _boom)
    resp = _tap(client, job)
    assert resp.status_code == 200, resp.text
    assert resp.json()["customer_notified"] is False
    assert _en_route_committed(db, job)
    assert _texts(db) == []
    [failed] = _audit(db, "on_my_way_sms_failed")
    assert failed.details["error"] == "RuntimeError"
    # The en-route audit trail the route wrote before the text is intact.
    assert _audit(db, "mobile_job_en_route")


@respx.mock
def test_phone_com_refusing_the_text_leaves_the_tap_alone(app_and_db):
    client, db = app_and_db
    respx.post(SEND_URL).mock(return_value=httpx.Response(500, json={"error": "down"}))
    job = _job(db)
    assert _tap(client, job).json()["customer_notified"] is True  # queued, honestly
    scheduled.drain(db, now=datetime.now(UTC) + timedelta(seconds=5))
    assert _en_route_committed(db, job)
    db.expire_all()
    row = db.get(ScheduledSms, _texts(db)[0].id)
    assert row.status in ("failed", "unknown")
    assert _audit(db, f"scheduled_sms_{row.status}")
    [bell] = db.execute(select(Notification)).scalars().all()
    assert "on-my-way text" in bell.message


# ── a tap replayed late from the offline queue ───────────────────────────


@respx.mock
def test_an_offline_tap_replayed_after_arrival_does_not_text(app_and_db):
    # No signal in the garage: the phone replays "On my way" and then "I'm
    # here" back to back once it reconnects, before the drain runs.
    client, db = app_and_db
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"id": "pc-1", "status": "sent"}))
    job = _job(db)
    assert _tap(client, job).json()["customer_notified"] is True
    arrived = client.post(f"/api/mobile/jobs/{job.id.hex}/arrived", json={})
    assert arrived.status_code == 200, arrived.text
    db.expire_all()
    assert db.get(Job, job.id).dispatch_status == "on_site"

    scheduled.drain(db, now=datetime.now(UTC) + timedelta(seconds=5))
    assert route.call_count == 0
    db.expire_all()
    row = db.get(ScheduledSms, _texts(db)[0].id)
    assert (row.status, row.error_code) == ("skipped", "arrived_before_send")
    [skip] = _audit(db, "scheduled_sms_skipped")
    assert skip.user_id == str(USER_ID)
    # Nothing for the office to act on: no bell.
    assert db.execute(select(Notification)).first() is None
