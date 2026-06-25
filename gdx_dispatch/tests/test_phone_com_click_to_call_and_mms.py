"""Click-to-call (outbound origination) + inbound MMS media proxy.

Both mirror the existing audio-proxy test harness: a stand-alone app with
auth + module gate overridden, respx for the upstream Phone.com calls.
"""
from __future__ import annotations

import json
from uuid import uuid4

import httpx
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import pytest

from gdx_dispatch.control.models import Base as ControlBase
from gdx_dispatch.control.models import Tenant
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db, get_tenant_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.modules.phone_com import key_storage
from gdx_dispatch.modules.phone_com.models import PhoneComMessage
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


def _make_app(control_engine, tenant_engine, tid):
    app = FastAPI()
    app.include_router(ops_router)
    csm = sessionmaker(bind=control_engine, expire_on_commit=False)
    tsm = sessionmaker(bind=tenant_engine, expire_on_commit=False)

    def fake_user():
        return {"user_id": str(uuid4()), "role": "admin", "tenant_id": str(tid)}

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
    app.dependency_overrides[get_tenant_db] = fake_tenant_db
    app.dependency_overrides[require_module("phone_com")] = lambda: None
    return app, csm, tsm


def _seed_token_and_settings(control_engine, tenant_engine, tenant_id, **app_kw):
    cs = sessionmaker(bind=control_engine, expire_on_commit=False)()
    key_storage.set_token(cs, tenant_id, "phc-token")
    cs.close()
    ts = sessionmaker(bind=tenant_engine, expire_on_commit=False)()
    ts.add(AppSettings(phone_com_voip_id="1000000", **app_kw))
    ts.commit()
    ts.close()


# ── click-to-call ──────────────────────────────────────────────────────


@respx.mock
def test_originate_call_bridges_extension_and_customer(
    control_engine, tenant_engine, tenant_id,
):
    _seed_token_and_settings(
        control_engine, tenant_engine, tenant_id,
        phone_com_default_extension_id="42",
        phone_com_default_caller_id="+18005550199",
    )
    route = respx.post("https://api.phone.com/v4/accounts/1000000/calls").mock(
        return_value=httpx.Response(201, json={"id": "call-123", "status": "ringing"})
    )
    app, _, _ = _make_app(control_engine, tenant_engine, tenant_id)
    r = TestClient(app).post(
        "/api/phone-com/calls/originate", json={"to": "(320) 295-9628"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ringing"
    # Payload: ring extension 42, dial the normalized customer number, present
    # the tenant caller-id.
    sent = json.loads(route.calls.last.request.content)
    assert sent["caller_extension"] == 42
    assert sent["callee_phone_number"] == "+13202959628"
    assert sent["callee_caller_id"] == "+18005550199"


def test_originate_call_503_without_extension(
    control_engine, tenant_engine, tenant_id,
):
    _seed_token_and_settings(control_engine, tenant_engine, tenant_id)  # no extension
    app, _, _ = _make_app(control_engine, tenant_engine, tenant_id)
    r = TestClient(app).post("/api/phone-com/calls/originate", json={"to": "+13202959628"})
    assert r.status_code == 503


# ── inbound MMS media proxy ────────────────────────────────────────────


def _seed_message(tsm, attachments):
    s = tsm()
    m = PhoneComMessage(
        phone_com_message_id=f"m-{uuid4()}",
        thread_key="t", direction="in",
        from_number="+13202959628", to_number="+18005550199",
        attachments=attachments, raw_payload={},
    )
    s.add(m)
    s.commit()
    s.refresh(m)
    mid = m.id
    s.close()
    return mid


@respx.mock
def test_mms_media_proxy_streams_and_hides_url(
    control_engine, tenant_engine, tenant_id,
):
    _seed_token_and_settings(control_engine, tenant_engine, tenant_id)
    media_url = "https://mds.phone.com/mms/img.jpg"
    respx.get(media_url).mock(
        return_value=httpx.Response(
            200, content=b"JPEG-bytes",
            headers={"content-type": "image/jpeg"},
        )
    )
    app, _, tsm = _make_app(control_engine, tenant_engine, tenant_id)
    mid = _seed_message(tsm, [{"url": media_url}])

    r = TestClient(app).get(f"/api/phone-com/messages/{mid}/media/0")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert r.content == b"JPEG-bytes"
    assert "mds.phone.com" not in r.text  # upstream URL never leaks


def test_mms_media_index_out_of_range_404(
    control_engine, tenant_engine, tenant_id,
):
    _seed_token_and_settings(control_engine, tenant_engine, tenant_id)
    app, _, tsm = _make_app(control_engine, tenant_engine, tenant_id)
    mid = _seed_message(tsm, [])  # no attachments
    r = TestClient(app).get(f"/api/phone-com/messages/{mid}/media/0")
    assert r.status_code == 404
