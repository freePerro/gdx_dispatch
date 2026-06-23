"""pc-s8 — Phone.com Settings router tests.

Mounts a stand-alone FastAPI app with the phone_com_settings router and
overrides the auth + DB dependencies with sqlite test sessions. This
isolates the router contract from the production app's middleware stack
(JWT validation, tenant routing) which would otherwise require a full
boot for every test.
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
from gdx_dispatch.modules.phone_com import key_storage
from gdx_dispatch.modules.phone_com.client import BASE_URL
from gdx_dispatch.routers import phone_com_settings

_ACCT = {
    "filters": {}, "sort": {"id": "desc"}, "total": 1, "limit": 25, "offset": None,
    "items": [{
        "id": 1000000, "name": "Example Owner",
        "username": "doug@example.com", "timezone": "America/Chicago",
        "features": {"call-recording-on": False},
    }],
}


@pytest.fixture(autouse=True)
def fernet_env(monkeypatch):
    monkeypatch.setenv("GDX_FERNET_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    """sqlite test DB doesn't have the prod audit_logs guard schema."""
    monkeypatch.setattr(
        "gdx_dispatch.routers.phone_com_settings.log_audit_event_sync",
        lambda *a, **kw: None,
        raising=False,
    )
    monkeypatch.setattr(
        "gdx_dispatch.modules.phone_com.key_storage.log_audit_event_sync",
        lambda *a, **kw: None,
        raising=False,
    )


@pytest.fixture
def control_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Direct Table.create per table — ControlBase.metadata.create_all has
    # FK validation that trips on tables registered by other modules
    # (installations, etc.) whose definitions aren't loaded here.
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
def tenant_id_with_tenant_row(control_engine):
    sm = sessionmaker(bind=control_engine, expire_on_commit=False)
    sess = sm()
    tid = uuid4()
    sess.add(Tenant(id=tid, slug="test-tenant", name="Test"))
    sess.commit()
    sess.close()
    return tid


def _make_app(control_engine, tenant_engine, *, role: str = "admin",
              tenant_id) -> FastAPI:
    app = FastAPI()
    app.include_router(phone_com_settings.router)

    control_sm = sessionmaker(bind=control_engine, expire_on_commit=False)
    tenant_sm = sessionmaker(bind=tenant_engine, expire_on_commit=False)

    def fake_user():
        return {"user_id": "u-1", "role": role, "tenant_id": str(tenant_id)}

    def fake_control_db():
        s = control_sm()
        try:
            yield s
        finally:
            s.close()

    def fake_tenant_db():
        s = tenant_sm()
        try:
            yield s
        finally:
            s.close()

    # test_and_cache_account calls _SessionLocal() directly (Phase D); patch it
    # to the same in-memory engine so AppSettings writes land in the test DB.
    import gdx_dispatch.modules.phone_com.key_storage as ks
    ks._SessionLocal = tenant_sm  # type: ignore

    app.dependency_overrides[get_current_user] = fake_user
    app.dependency_overrides[get_db] = fake_control_db
    app.dependency_overrides[get_tenant_db] = fake_tenant_db
    return app


def _client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ── GET ─────────────────────────────────────────────────────────────────


def test_get_returns_unset_state(control_engine, tenant_engine, tenant_id_with_tenant_row):
    app = _make_app(control_engine, tenant_engine, tenant_id=tenant_id_with_tenant_row)
    r = _client(app).get("/api/settings/integrations/phone-com")
    assert r.status_code == 200
    body = r.json()
    assert body["token_set"] is False
    assert body["voip_id"] is None
    assert body["webhook_status"]["registered"] is False
    assert body["account_features"] is None


def test_get_never_returns_token(
    control_engine, tenant_engine, tenant_id_with_tenant_row,
):
    SECRET = "phc-secret-token-MUST-NOT-LEAK-12345"
    sm = sessionmaker(bind=control_engine, expire_on_commit=False)
    s = sm()
    key_storage.set_token(s, tenant_id_with_tenant_row, SECRET)
    s.close()

    app = _make_app(control_engine, tenant_engine, tenant_id=tenant_id_with_tenant_row)
    r = _client(app).get("/api/settings/integrations/phone-com")
    assert r.status_code == 200
    assert SECRET not in r.text
    assert r.json()["token_set"] is True


# ── PATCH ───────────────────────────────────────────────────────────────


def test_patch_non_admin_returns_403(
    control_engine, tenant_engine, tenant_id_with_tenant_row,
):
    app = _make_app(
        control_engine, tenant_engine, tenant_id=tenant_id_with_tenant_row, role="technician",
    )
    r = _client(app).patch(
        "/api/settings/integrations/phone-com", json={"voip_id": 1000000},
    )
    assert r.status_code == 403


@respx.mock
def test_patch_invalid_token_returns_400(
    control_engine, tenant_engine, tenant_id_with_tenant_row,
):
    respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(401, json={"error": "invalid"})
    )
    app = _make_app(
        control_engine, tenant_engine, tenant_id=tenant_id_with_tenant_row,
    )
    r = _client(app).patch(
        "/api/settings/integrations/phone-com",
        json={"token": "phc-bad-token", "voip_id": 1000000},
    )
    assert r.status_code == 400
    assert "401" in r.text or "invalid" in r.text.lower()


@respx.mock
def test_patch_valid_token_registers_webhook(
    control_engine, tenant_engine, tenant_id_with_tenant_row,
):
    respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(200, json=_ACCT)
    )
    cb_route = respx.post(
        f"{BASE_URL}/accounts/1000000/integrations/events/callbacks"
    ).mock(
        return_value=httpx.Response(200, json={"id": 555, "config": {"url": "x"}})
    )
    listener_route = respx.post(
        f"{BASE_URL}/accounts/1000000/integrations/events/listeners"
    ).mock(
        return_value=httpx.Response(200, json={"id": 777, "callback_id": 555})
    )
    # ensure_webhook also lists existing callbacks first
    respx.get(
        f"{BASE_URL}/accounts/1000000/integrations/events/callbacks"
    ).mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0})
    )

    app = _make_app(
        control_engine, tenant_engine, tenant_id=tenant_id_with_tenant_row,
    )
    r = _client(app).patch(
        "/api/settings/integrations/phone-com",
        json={"token": "phc-good-token", "voip_id": 1000000},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_set"] is True
    assert body["test_result"]["ok"] is True
    assert body["webhook_status"]["registered"] is True
    assert body["webhook_status"]["callback_id"] == 555
    assert body["webhook_status"]["listener_id"] == 777
    assert cb_route.call_count == 1
    assert listener_route.call_count == 1


def test_patch_rejects_garbage_caller_id(
    control_engine, tenant_engine, tenant_id_with_tenant_row,
):
    app = _make_app(
        control_engine, tenant_engine, tenant_id=tenant_id_with_tenant_row,
    )
    r = _client(app).patch(
        "/api/settings/integrations/phone-com",
        json={"default_caller_id": "not-a-number"},
    )
    assert r.status_code == 422


# ── DELETE /token ───────────────────────────────────────────────────────


def test_delete_clears_token(
    control_engine, tenant_engine, tenant_id_with_tenant_row,
):
    sm = sessionmaker(bind=control_engine, expire_on_commit=False)
    s = sm()
    key_storage.set_token(s, tenant_id_with_tenant_row, "phc-token")
    s.close()

    app = _make_app(
        control_engine, tenant_engine, tenant_id=tenant_id_with_tenant_row,
    )
    r = _client(app).delete("/api/settings/integrations/phone-com/token")
    assert r.status_code == 200
    assert r.json()["cleared"] is True

    # GET shows token_set=False afterwards
    r2 = _client(app).get("/api/settings/integrations/phone-com")
    assert r2.json()["token_set"] is False


# ── POST /test ──────────────────────────────────────────────────────────


@respx.mock
def test_test_endpoint_no_token(
    control_engine, tenant_engine, tenant_id_with_tenant_row,
):
    app = _make_app(
        control_engine, tenant_engine, tenant_id=tenant_id_with_tenant_row,
    )
    r = _client(app).post("/api/settings/integrations/phone-com/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "no token" in body["error"]


@respx.mock
def test_test_endpoint_with_token(
    control_engine, tenant_engine, tenant_id_with_tenant_row,
):
    sm = sessionmaker(bind=control_engine, expire_on_commit=False)
    s = sm()
    key_storage.set_token(s, tenant_id_with_tenant_row, "phc-good")
    s.close()

    respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(200, json=_ACCT)
    )

    app = _make_app(
        control_engine, tenant_engine, tenant_id=tenant_id_with_tenant_row,
    )
    r = _client(app).post("/api/settings/integrations/phone-com/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["voip_id"] == 1000000


# ── POST /sync-now ──────────────────────────────────────────────────────


def test_sync_now_runs_resync_inline(
    monkeypatch, control_engine, tenant_engine, tenant_id_with_tenant_row,
):
    """sync-now runs the backfill synchronously and returns counts."""
    monkeypatch.setattr(
        "gdx_dispatch.routers.phone_com_settings._run_resync_sync",
        lambda tid, db: {
            "ok": True, "calls_synced": 7, "messages_synced": 3, "voicemails_synced": 1,
        },
    )
    app = _make_app(
        control_engine, tenant_engine, tenant_id=tenant_id_with_tenant_row,
    )
    r = _client(app).post("/api/settings/integrations/phone-com/sync-now")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["calls_synced"] == 7
    assert body["messages_synced"] == 3


def test_sync_now_502_on_upstream_error(
    monkeypatch, control_engine, tenant_engine, tenant_id_with_tenant_row,
):
    monkeypatch.setattr(
        "gdx_dispatch.routers.phone_com_settings._run_resync_sync",
        lambda tid, db: {"ok": False, "error": "401 oauth2.access_denied"},
    )
    app = _make_app(
        control_engine, tenant_engine, tenant_id=tenant_id_with_tenant_row,
    )
    r = _client(app).post("/api/settings/integrations/phone-com/sync-now")
    assert r.status_code == 502
    assert "401" in r.text


def test_sync_now_admin_only(
    control_engine, tenant_engine, tenant_id_with_tenant_row,
):
    app = _make_app(
        control_engine, tenant_engine, tenant_id=tenant_id_with_tenant_row,
        role="technician",
    )
    r = _client(app).post("/api/settings/integrations/phone-com/sync-now")
    assert r.status_code == 403


# ── webhook URL builder ─────────────────────────────────────────────────


def test_build_webhook_url_uses_tenant_slug_and_secret():
    url = phone_com_settings._build_webhook_url("acme-co", "s3cr3t")
    assert "acme-co" in url
    assert "s3cr3t" in url
    assert url.startswith("https://acme-co.")
    assert "/api/webhooks/phone-com/acme-co/s3cr3t" in url
