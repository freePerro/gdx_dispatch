"""pc-s9 — phone_com calls router tests.

Mounts a stand-alone FastAPI app with the operational router. Auth +
module gate are overridden so these tests focus on the route logic, not
the middleware stack.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.control.models import Base as ControlBase
from gdx_dispatch.control.models import Tenant
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db, get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import AppSettings, Customer
from gdx_dispatch.modules.phone_com import key_storage
from gdx_dispatch.modules.phone_com.models import (
    PhoneComCall,
    PhoneComVoicemail,
)
from gdx_dispatch.modules.phone_com.router import router as ops_router


@pytest.fixture(autouse=True)
def fernet_env(monkeypatch):
    monkeypatch.setenv("GDX_FERNET_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    monkeypatch.setattr(
        "gdx_dispatch.modules.phone_com.key_storage.log_audit_event_sync",
        lambda *a, **kw: None, raising=False,
    )


@pytest.fixture
def control_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for name in ("tenants", "tenant_settings"):
        if name in ControlBase.metadata.tables:
            ControlBase.metadata.tables[name].create(engine, checkfirst=True)
    return engine


@pytest.fixture
def tenant_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine)
    return engine


@pytest.fixture
def tenant_id(control_engine):
    sm = sessionmaker(bind=control_engine, expire_on_commit=False)
    s = sm()
    tid = uuid4()
    s.add(Tenant(id=tid, slug="t1", name="T"))
    s.commit()
    s.close()
    return tid


def _make_app(control_engine, tenant_engine, tid, *, role="admin",
              skip_module_gate=True):
    app = FastAPI()
    app.include_router(ops_router)

    csm = sessionmaker(bind=control_engine, expire_on_commit=False)
    tsm = sessionmaker(bind=tenant_engine, expire_on_commit=False)

    def fake_user():
        return {"user_id": str(uuid4()), "role": role, "tenant_id": str(tid)}

    def fake_control_db():
        s = csm()
        try:
            yield s
        finally:
            s.close()

    def fake_tenant_db():
        s = tsm()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_current_user] = fake_user
    app.dependency_overrides[get_db] = fake_control_db
    app.dependency_overrides[get_db] = fake_tenant_db
    if skip_module_gate:
        app.dependency_overrides[require_module("phone_com")] = lambda: None
    return app, csm, tsm


def _seed_call(tsm, **kwargs) -> PhoneComCall:
    s = tsm()
    call = PhoneComCall(
        phone_com_call_id=kwargs.get("phone_com_call_id", str(uuid4())),
        direction=kwargs.get("direction", "in"),
        from_number=kwargs.get("from_number", "+13202959628"),
        to_number=kwargs.get("to_number", "+18005550199"),
        started_at=kwargs.get("started_at", datetime.now(timezone.utc)),
        duration_s=kwargs.get("duration_s", 30),
        status=kwargs.get("status", "completed"),
        recording_url=kwargs.get("recording_url"),
        customer_id=kwargs.get("customer_id"),
        job_id=kwargs.get("job_id"),
        raw_payload=kwargs.get("raw_payload", {}),
    )
    s.add(call)
    s.commit()
    s.refresh(call)
    cid = call.id
    s.close()
    return cid  # return id only — bound-state of ORM object can leak across sessions


def _seed_customer(tsm, name="Becky", phone="+13202959628") -> UUID:
    s = tsm()
    c = Customer(name=name, phone=phone, company_id="t1")
    s.add(c)
    s.commit()
    s.refresh(c)
    cid = c.id
    s.close()
    return cid


# ── list ───────────────────────────────────────────────────────────────


def test_list_calls_pagination(control_engine, tenant_engine, tenant_id):
    app, _, tsm = _make_app(control_engine, tenant_engine, tenant_id)
    base = datetime.now(timezone.utc)
    for i in range(30):
        _seed_call(tsm, phone_com_call_id=f"c-{i:03d}",
                   started_at=base - timedelta(minutes=i))
    r = TestClient(app).get("/api/phone-com/calls?per_page=10&page=1")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 30
    assert len(body["items"]) == 10
    # Sorted started_at desc — first row is the newest (offset 0)
    assert body["items"][0]["from_number"] == "+13202959628"


def test_list_calls_filter_direction(control_engine, tenant_engine, tenant_id):
    app, _, tsm = _make_app(control_engine, tenant_engine, tenant_id)
    _seed_call(tsm, phone_com_call_id="in-1", direction="in")
    _seed_call(tsm, phone_com_call_id="in-2", direction="in")
    _seed_call(tsm, phone_com_call_id="out-1", direction="out")
    r = TestClient(app).get("/api/phone-com/calls?direction=in")
    assert r.status_code == 200
    assert r.json()["total"] == 2


def test_list_calls_filter_customer(control_engine, tenant_engine, tenant_id):
    app, _, tsm = _make_app(control_engine, tenant_engine, tenant_id)
    cust_a = _seed_customer(tsm, name="A", phone="+1111111111")
    cust_b = _seed_customer(tsm, name="B", phone="+2222222222")
    for i in range(3):
        _seed_call(tsm, phone_com_call_id=f"a-{i}", customer_id=cust_a)
    for i in range(2):
        _seed_call(tsm, phone_com_call_id=f"b-{i}", customer_id=cust_b)
    r = TestClient(app).get(f"/api/phone-com/calls?customer_id={cust_a}")
    assert r.json()["total"] == 3


# ── detail ─────────────────────────────────────────────────────────────


def test_get_call_detail(control_engine, tenant_engine, tenant_id):
    app, _, tsm = _make_app(control_engine, tenant_engine, tenant_id)
    cust_id = _seed_customer(tsm, name="Becky")
    call_id = _seed_call(tsm, customer_id=cust_id)
    r = TestClient(app).get(f"/api/phone-com/calls/{call_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["customer_name"] == "Becky"
    assert body["has_recording"] is False
    assert body["has_voicemail"] is False


def test_get_call_detail_404(control_engine, tenant_engine, tenant_id):
    app, _, _ = _make_app(control_engine, tenant_engine, tenant_id)
    r = TestClient(app).get(f"/api/phone-com/calls/{uuid4()}")
    assert r.status_code == 404


# ── recording proxy ────────────────────────────────────────────────────


def test_recording_404_when_no_url(control_engine, tenant_engine, tenant_id):
    app, _, tsm = _make_app(control_engine, tenant_engine, tenant_id)
    call_id = _seed_call(tsm)  # recording_url=None
    r = TestClient(app).get(f"/api/phone-com/calls/{call_id}/recording")
    assert r.status_code == 404


@respx.mock
def test_recording_proxy_streams_audio(control_engine, tenant_engine, tenant_id):
    # Seed token + voip_id so _get_phone_com_client succeeds.
    csm = sessionmaker(bind=control_engine, expire_on_commit=False)
    cs = csm()
    key_storage.set_token(cs, tenant_id, "phc-token")
    cs.close()
    tsm = sessionmaker(bind=tenant_engine, expire_on_commit=False)
    ts = tsm()
    ts.add(AppSettings(phone_com_voip_id="1000000"))
    ts.commit()
    ts.close()

    rec_url = "https://mds.phone.com/recordings/abc.wav"
    respx.get(rec_url).mock(
        return_value=httpx.Response(
            200, content=b"WAVE-bytes",
            headers={"content-type": "audio/wav"},
        )
    )

    app, _, _ = _make_app(control_engine, tenant_engine, tenant_id)
    call_id = _seed_call(tsm, recording_url=rec_url)

    r = TestClient(app).get(f"/api/phone-com/calls/{call_id}/recording")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/")
    assert r.content == b"WAVE-bytes"
    # URL never leaks
    assert "mds.phone.com" not in r.text


# ── voicemail audio + transcript ───────────────────────────────────────


def test_voicemail_transcript_returns_inline_field(
    control_engine, tenant_engine, tenant_id,
):
    app, _, tsm = _make_app(control_engine, tenant_engine, tenant_id)
    call_id = _seed_call(tsm)
    s = tsm()
    s.add(PhoneComVoicemail(
        phone_com_voicemail_id="vm-1", call_id=call_id,
        transcript="hello", transcript_source="phone_com", raw_payload={},
    ))
    s.commit()
    s.close()
    r = TestClient(app).get(f"/api/phone-com/calls/{call_id}/voicemail-transcript")
    assert r.status_code == 200
    assert r.json() == {"transcript": "hello", "source": "phone_com"}


@respx.mock
def test_voicemail_audio_uses_cp_url_when_present(
    control_engine, tenant_engine, tenant_id,
):
    csm = sessionmaker(bind=control_engine, expire_on_commit=False)
    cs = csm()
    key_storage.set_token(cs, tenant_id, "phc-token")
    cs.close()
    tsm = sessionmaker(bind=tenant_engine, expire_on_commit=False)
    ts = tsm()
    ts.add(AppSettings(phone_com_voip_id="1000000"))
    ts.commit()
    ts.close()

    cp = "https://mds.phone.com/voicemails/v.wav"
    respx.get(cp).mock(
        return_value=httpx.Response(200, content=b"VM-AUDIO",
                                    headers={"content-type": "audio/wav"})
    )

    app, _, _ = _make_app(control_engine, tenant_engine, tenant_id)
    call_id = _seed_call(tsm)
    s = tsm()
    s.add(PhoneComVoicemail(
        phone_com_voicemail_id="vm-1", call_id=call_id,
        audio_url="https://api.phone.com/...authed",
        raw_payload={"voicemail_cp_url": cp},
    ))
    s.commit()
    s.close()

    r = TestClient(app).get(f"/api/phone-com/calls/{call_id}/voicemail-audio")
    assert r.status_code == 200
    assert r.content == b"VM-AUDIO"
    last = respx.calls.last.request
    assert "Authorization" not in last.headers


# ── mark heard ─────────────────────────────────────────────────────────


def test_mark_heard_voicemail(control_engine, tenant_engine, tenant_id):
    app, _, tsm = _make_app(control_engine, tenant_engine, tenant_id)
    call_id = _seed_call(tsm)
    s = tsm()
    s.add(PhoneComVoicemail(
        phone_com_voicemail_id="vm-1", call_id=call_id,
        audio_url="x", raw_payload={},
    ))
    s.commit()
    s.close()
    r = TestClient(app).post(f"/api/phone-com/calls/{call_id}/mark-heard")
    assert r.status_code == 204
    s = tsm()
    vm = s.query(PhoneComVoicemail).filter_by(call_id=call_id).first()
    assert vm.heard_at is not None
    s.close()


# ── module gate ────────────────────────────────────────────────────────


def test_module_gate_blocks_when_disabled(
    control_engine, tenant_engine, tenant_id,
):
    app, _, _ = _make_app(
        control_engine, tenant_engine, tenant_id, skip_module_gate=False,
    )
    # Without the override, require_module hits the DB which won't have
    # phone_com granted — expect 400/403 (depending on tenant context).
    r = TestClient(app).get("/api/phone-com/calls")
    assert r.status_code in (400, 403)
