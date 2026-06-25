"""In-app Phone.com diagnostics endpoint (Settings card).

GET /api/settings/integrations/phone-com/diagnostics — admin-only, read-only.
Verifies token/voip/webhook + the listener event-filter tag-match check.
"""
from __future__ import annotations

from uuid import uuid4

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
from gdx_dispatch.core.database import get_db, get_tenant_db
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.modules.phone_com import key_storage
from gdx_dispatch.modules.phone_com.client import BASE_URL
from gdx_dispatch.routers import phone_com_settings

LISTENERS_URL = f"{BASE_URL}/accounts/1000000/integrations/events/listeners"
FILTERS_URL = f"{LISTENERS_URL}/55/filters"


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
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    for name in ("tenants", "tenant_settings"):
        if name in ControlBase.metadata.tables:
            ControlBase.metadata.tables[name].create(engine, checkfirst=True)
    return engine


@pytest.fixture
def tenant_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
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


def _make_app(control_engine, tenant_engine, tid, *, role="admin"):
    app = FastAPI()
    app.include_router(phone_com_settings.router)
    csm = sessionmaker(bind=control_engine, expire_on_commit=False)
    tsm = sessionmaker(bind=tenant_engine, expire_on_commit=False)

    def fake_user():
        return {"user_id": "u-1", "role": role, "tenant_id": str(tid)}

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
    return app, csm, tsm


def _seed(csm, tsm, tid, *, token=True, voip="1000000"):
    if token:
        s = csm()
        key_storage.set_token(s, tid, "phc-token")
        s.close()
    if voip:
        t = tsm()
        t.add(AppSettings(phone_com_voip_id=voip))
        t.commit()
        t.close()


def test_diagnostics_requires_admin(control_engine, tenant_engine, tenant_id):
    app, _, _ = _make_app(control_engine, tenant_engine, tenant_id, role="tech")
    r = TestClient(app).get("/api/settings/integrations/phone-com/diagnostics")
    assert r.status_code == 403


def test_diagnostics_fails_without_token(control_engine, tenant_engine, tenant_id):
    app, csm, tsm = _make_app(control_engine, tenant_engine, tenant_id)
    _seed(csm, tsm, tenant_id, token=False)
    r = TestClient(app).get("/api/settings/integrations/phone-com/diagnostics")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["checks"][0]["key"] == "token"
    assert body["checks"][0]["status"] == "fail"


@respx.mock
def test_diagnostics_warns_on_phone_prefixed_filter_tags(
    control_engine, tenant_engine, tenant_id,
):
    app, csm, tsm = _make_app(control_engine, tenant_engine, tenant_id)
    _seed(csm, tsm, tenant_id)
    respx.get(LISTENERS_URL).mock(return_value=httpx.Response(
        200, json={"items": [{"id": 55, "callback_id": 7}], "total": 1},
    ))
    respx.get(FILTERS_URL).mock(return_value=httpx.Response(
        200, json={"items": [{"field": "type", "operator": "in",
                              "value": ["phone.call", "phone.voicemail"]}], "total": 1},
    ))
    r = TestClient(app).get("/api/settings/integrations/phone-com/diagnostics")
    assert r.status_code == 200
    body = r.json()
    tags = next(c for c in body["checks"] if c["key"] == "filter_tags")
    # Our phone.* values are flagged: they may match none of Phone.com's bare tags.
    assert tags["status"] == "warn"
    assert tags["registered"] == ["phone.call", "phone.voicemail"]


@respx.mock
def test_diagnostics_listener_error_hides_upstream_body(
    control_engine, tenant_engine, tenant_id,
):
    app, csm, tsm = _make_app(control_engine, tenant_engine, tenant_id)
    _seed(csm, tsm, tenant_id)
    # Upstream 401 with a body that must NOT leak into the response.
    respx.get(LISTENERS_URL).mock(return_value=httpx.Response(
        401, text="SECRET-UPSTREAM-BODY oauth2.access_denied",
    ))
    r = TestClient(app).get("/api/settings/integrations/phone-com/diagnostics")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    listeners = next(c for c in body["checks"] if c["key"] == "listeners")
    assert listeners["status"] == "fail"
    assert "HTTP 401" in listeners["detail"]
    assert "SECRET-UPSTREAM-BODY" not in r.text  # upstream body never exposed


@respx.mock
def test_diagnostics_warns_when_no_listeners(
    control_engine, tenant_engine, tenant_id,
):
    app, csm, tsm = _make_app(control_engine, tenant_engine, tenant_id)
    _seed(csm, tsm, tenant_id)
    respx.get(LISTENERS_URL).mock(return_value=httpx.Response(200, json={"items": [], "total": 0}))
    r = TestClient(app).get("/api/settings/integrations/phone-com/diagnostics")
    assert r.status_code == 200
    body = r.json()
    listeners = next(c for c in body["checks"] if c["key"] == "listeners")
    assert listeners["status"] == "warn"
