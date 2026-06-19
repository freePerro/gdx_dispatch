"""pc-s12 — phone_com webhook receiver tests."""
from __future__ import annotations

from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.control.models import Base as ControlBase
from gdx_dispatch.control.models import Tenant
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import AppSettings, Customer
from gdx_dispatch.modules.phone_com import key_storage
from gdx_dispatch.modules.phone_com import webhook_router as wr
from gdx_dispatch.modules.phone_com.models import (
    PhoneComCall,
    PhoneComMessage,
    PhoneComVoicemail,
)


@pytest.fixture(autouse=True)
def fernet_env(monkeypatch):
    monkeypatch.setenv("GDX_FERNET_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    monkeypatch.setattr(
        "gdx_dispatch.modules.phone_com.key_storage.log_audit_event_sync",
        lambda *a, **kw: None, raising=False,
    )
    monkeypatch.setattr(
        "gdx_dispatch.modules.phone_com.webhook_router.log_audit_event_sync",
        lambda *a, **kw: None, raising=False,
    )


@pytest.fixture
def unified_engine():
    # Phase C: single DB for control + tenant tables.
    e = create_engine("sqlite:///:memory:",
                      connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    ControlBase.metadata.create_all(e, checkfirst=True)
    TenantBase.metadata.create_all(e, checkfirst=True)
    from gdx_dispatch.models.tenant_models import Base as TenantModelsBase
    TenantModelsBase.metadata.create_all(e, checkfirst=True)
    return e


@pytest.fixture
def setup(unified_engine, monkeypatch):
    """Seed a tenant + voip_id + webhook secret into the unified DB."""
    sm = sessionmaker(bind=unified_engine, expire_on_commit=False)
    s = sm()
    tid = uuid4()
    s.add(Tenant(id=tid, slug="t1", name="T"))
    s.commit()
    secret = key_storage.get_or_create_webhook_secret(s, tid)
    s.add(AppSettings(phone_com_voip_id="1000000"))
    s.commit()
    s.close()

    monkeypatch.setattr(wr, "SessionLocal", sm)
    monkeypatch.setattr(wr, "_open_tenant_session", lambda *_: sm())
    # Phase C: webhook_router calls single_tenant() for tenant resolution.
    # Pin it to the test tenant so TenantSettings lookups find the right row.
    monkeypatch.setattr("gdx_dispatch.core.tenant.single_tenant",
                        lambda: {"id": str(tid), "slug": "t1", "db_url": ""})

    app = FastAPI()
    app.include_router(wr.router)
    return app, sm, sm, tid, secret


# ── short-circuit + auth ──────────────────────────────────────────────


def test_test_ping_short_circuits_204(setup):
    app, _, _, _, _ = setup
    # Bad slug + bad secret — must still 204 because body is the test ping.
    r = TestClient(app).post(
        "/api/webhooks/phone-com/no-such-tenant/garbage",
        json={"test": 1},
    )
    assert r.status_code == 204


def test_unknown_tenant_slug_returns_404(setup):
    app, _, _, _, _ = setup
    r = TestClient(app).post(
        "/api/webhooks/phone-com/no-such-tenant/anything",
        json={"voip_id": 1},
    )
    assert r.status_code == 404


def test_bad_path_secret_returns_404(setup):
    app, _, _, tid, _ = setup
    r = TestClient(app).post(
        "/api/webhooks/phone-com/t1/wrong-secret",
        json={"voip_id": 1000000, "type": "call.completed", "id": "c1"},
    )
    assert r.status_code == 404


def test_voip_id_mismatch_returns_400(setup):
    app, _, _, _, secret = setup
    r = TestClient(app).post(
        f"/api/webhooks/phone-com/t1/{secret}",
        json={"voip_id": 99999, "type": "call.completed", "id": "c1"},
    )
    assert r.status_code == 400


# ── call upsert ────────────────────────────────────────────────────────


def test_call_completed_upserts_row(setup):
    app, _, tsm, _, secret = setup
    payload = {
        "voip_id": 1000000, "type": "call.completed",
        "id": "phc-call-001", "direction": "in",
        "caller_id": "+13202959628", "called_number": "+18005550199",
        "duration": 30, "status": "completed",
    }
    r = TestClient(app).post(
        f"/api/webhooks/phone-com/t1/{secret}", json=payload,
    )
    assert r.status_code == 204
    s = tsm()
    rows = s.query(PhoneComCall).all()
    assert len(rows) == 1
    assert rows[0].phone_com_call_id == "phc-call-001"
    assert rows[0].direction == "in"
    assert rows[0].duration_s == 30
    s.close()


def test_call_completed_resolves_customer(setup):
    """Inbound call from a known phone hashes to a Customer row → customer_id populated."""
    app, _, tsm, _, secret = setup
    s = tsm()
    cust = Customer(name="Becky", phone="+13202959628", company_id="t1")
    s.add(cust)
    s.commit()
    cust_id = cust.id
    s.close()

    payload = {
        "voip_id": 1000000, "type": "call.completed",
        "id": "phc-call-002", "direction": "in",
        "caller_id": "+13202959628", "called_number": "+18005550199",
    }
    r = TestClient(app).post(
        f"/api/webhooks/phone-com/t1/{secret}", json=payload,
    )
    assert r.status_code == 204
    s = tsm()
    row = s.query(PhoneComCall).filter_by(phone_com_call_id="phc-call-002").first()
    assert row.customer_id == cust_id
    s.close()


def test_call_upsert_idempotent(setup):
    app, _, tsm, _, secret = setup
    payload = {
        "voip_id": 1000000, "type": "call.completed",
        "id": "phc-dup", "direction": "in",
        "caller_id": "+1", "duration": 10,
    }
    for _ in range(3):
        r = TestClient(app).post(
            f"/api/webhooks/phone-com/t1/{secret}", json=payload,
        )
        assert r.status_code == 204
    s = tsm()
    assert s.query(PhoneComCall).count() == 1
    s.close()


# ── message upsert ─────────────────────────────────────────────────────


def test_sms_received_upserts_message(setup):
    app, _, tsm, _, secret = setup
    payload = {
        "voip_id": 1000000, "type": "sms.received",
        "id": "phc-msg-001", "direction": "in",
        "from": "+13202959628", "to": "+18005550199", "text": "hi",
    }
    r = TestClient(app).post(
        f"/api/webhooks/phone-com/t1/{secret}", json=payload,
    )
    assert r.status_code == 204
    s = tsm()
    row = s.query(PhoneComMessage).filter_by(phone_com_message_id="phc-msg-001").first()
    assert row is not None
    assert row.body == "hi"
    assert row.direction == "in"
    assert row.thread_key == "+13202959628|+18005550199"
    s.close()


# ── voicemail upsert ───────────────────────────────────────────────────


def test_voicemail_created_upserts_and_links_to_call(setup):
    app, _, tsm, _, secret = setup
    s = tsm()
    call = PhoneComCall(
        phone_com_call_id="phc-call-vm", direction="in",
        from_number="+1", raw_payload={},
    )
    s.add(call)
    s.commit()
    call_id = call.id
    s.close()

    payload = {
        "voip_id": 1000000, "type": "voicemail.created",
        "id": "phc-vm-001", "call_id": "phc-call-vm",
        "audio_url": "https://api.phone.com/.../vm.wav",
        "transcript": "hello", "duration": 12,
    }
    r = TestClient(app).post(
        f"/api/webhooks/phone-com/t1/{secret}", json=payload,
    )
    assert r.status_code == 204
    s = tsm()
    vm = s.query(PhoneComVoicemail).filter_by(phone_com_voicemail_id="phc-vm-001").first()
    assert vm is not None
    assert vm.call_id == call_id
    assert vm.transcript == "hello"
    s.close()


# ── unknown event ──────────────────────────────────────────────────────


def test_unknown_event_returns_204_no_row(setup):
    app, _, tsm, _, secret = setup
    r = TestClient(app).post(
        f"/api/webhooks/phone-com/t1/{secret}",
        json={"voip_id": 1000000, "type": "weird.unknown.event", "id": "x"},
    )
    # 204 even on unknown event types — Phone.com retries on 5xx.
    assert r.status_code == 204
    s = tsm()
    assert s.query(PhoneComCall).count() == 0
    assert s.query(PhoneComMessage).count() == 0
    s.close()


def test_empty_body_returns_204(setup):
    app, _, _, _, secret = setup
    r = TestClient(app).post(
        f"/api/webhooks/phone-com/t1/{secret}",
        content="",
    )
    assert r.status_code == 204
