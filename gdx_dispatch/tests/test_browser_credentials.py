"""Remembered-login (autofill) credential endpoints, both layers:

* plugin-host internal store — real files in a tmp dir: save/status/merge/
  delete, and the invariant that the password NEVER appears in a response.
* core proxy gates — owner role + live "browser" permission + consent must all
  hold before anything is forwarded to the plugin-host (upstream mocked).

Needs FastAPI → runs in the docker image.
"""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.plugin_host.app import create_plugin_host
from gdx_dispatch.routers import browser_proxy
from gdx_dispatch.routers.auth import get_current_user


# ── plugin-host internal store ───────────────────────────────────────────────

def _host_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("PLUGIN_BROWSER_STATE_DIR", str(tmp_path))
    return TestClient(create_plugin_host(plugins=[]))


def test_host_creds_roundtrip_never_leaks_password(tmp_path, monkeypatch):
    c = _host_client(tmp_path, monkeypatch)
    r = c.post("/internal/browser/credentials",
               json={"key": "chipricing", "username": "doug@x.com", "password": "pw-1"})
    assert r.status_code == 200 and r.json() == {"saved": True, "username": "doug@x.com"}

    r = c.get("/internal/browser/credentials", params={"key": "chipricing"})
    assert r.json() == {"saved": True, "username": "doug@x.com", "has_password": True}
    assert "pw-1" not in r.text  # the password never leaves the store

    r = c.delete("/internal/browser/credentials", params={"key": "chipricing"})
    assert r.json() == {"saved": False}
    assert c.get("/internal/browser/credentials",
                 params={"key": "chipricing"}).json() == {"saved": False}


def test_host_creds_blank_password_keeps_stored_one(tmp_path, monkeypatch):
    c = _host_client(tmp_path, monkeypatch)
    c.post("/internal/browser/credentials",
           json={"key": "chipricing", "username": "doug@x.com", "password": "pw-1"})
    # Re-save with a new username and a blank password (UI shows "unchanged").
    r = c.post("/internal/browser/credentials",
               json={"key": "chipricing", "username": "new@x.com", "password": ""})
    assert r.json()["username"] == "new@x.com"
    from gdx_dispatch.plugin_host import browser_stream as bs
    saved = bs.load_state(bs.creds_file_for("chipricing"))
    assert saved == {"username": "new@x.com", "password": "pw-1"}


def test_host_creds_rejects_bad_or_empty(tmp_path, monkeypatch):
    c = _host_client(tmp_path, monkeypatch)
    assert c.post("/internal/browser/credentials",
                  json={"key": "!!!", "username": "u", "password": "p"}).status_code == 400
    assert c.post("/internal/browser/credentials",
                  json={"key": "chipricing", "username": "", "password": ""}).status_code == 400
    assert not list(tmp_path.iterdir())  # nothing persisted


# ── core proxy gates ─────────────────────────────────────────────────────────

class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeClient:
    """Records the request the proxy would send to the plugin-host."""

    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, **kwargs):
        _FakeClient.captured = {"method": method, "url": url, **kwargs}
        return _FakeResp({"saved": True, "username": "doug@x.com"})


def _core_client(monkeypatch, *, role="owner", permissions=("browser",), consent=True):
    monkeypatch.setattr(browser_proxy.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(browser_proxy, "fetch_permissions", lambda key: set(permissions))
    monkeypatch.setattr(browser_proxy, "has_permission_consent", lambda db, key, perm: consent)
    app = FastAPI()
    app.include_router(browser_proxy.router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": 1, "role": role}
    # A real session, not iter([None]): these endpoints record who asked for a
    # credential change, so the audit write is part of the contract now and a
    # stub session would hide whether it lands.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    app.dependency_overrides[browser_proxy.audit_ready_db] = lambda: session
    client = TestClient(app)
    client.audit_session = session  # so tests can assert on the trail
    return client


def _audit_actions(client) -> list[str]:
    return [r.action for r in client.audit_session.execute(select(AuditLog)).scalars().all()]


def test_core_creds_forwarded_for_owner(monkeypatch):
    c = _core_client(monkeypatch)
    r = c.post("/api/plugins/_browser/credentials",
               json={"key": "chipricing", "username": "doug@x.com", "password": "pw-1"})
    assert r.status_code == 200 and r.json()["saved"] is True
    assert _FakeClient.captured["method"] == "POST"
    assert _FakeClient.captured["json"]["password"] == "pw-1"

    assert c.get("/api/plugins/_browser/credentials",
                 params={"key": "chipricing"}).status_code == 200
    assert c.delete("/api/plugins/_browser/credentials",
                    params={"key": "chipricing"}).status_code == 200

    # Storing and revoking a remembered login are recorded — named for what is
    # known at write time (a request), since the plugin-host store can still fail.
    actions = _audit_actions(c)
    assert "plugin.browser_credentials_save_requested" in actions
    assert "plugin.browser_credentials_forget_requested" in actions
    # The password must never reach the audit trail.
    details = [r.details for r in c.audit_session.execute(select(AuditLog)).scalars().all()]
    assert not any("pw-1" in json.dumps(d or {}) for d in details)


def test_core_creds_gates_block(monkeypatch):
    body = {"key": "chipricing", "username": "u", "password": "p"}
    assert _core_client(monkeypatch, role="admin").post(
        "/api/plugins/_browser/credentials", json=body).status_code == 403
    assert _core_client(monkeypatch, permissions=()).post(
        "/api/plugins/_browser/credentials", json=body).status_code == 403
    assert _core_client(monkeypatch, consent=False).post(
        "/api/plugins/_browser/credentials", json=body).status_code == 403
    # Empty credentials are rejected before any upstream call.
    assert _core_client(monkeypatch).post(
        "/api/plugins/_browser/credentials",
        json={"key": "chipricing", "username": "", "password": ""}).status_code == 400


def test_ticket_still_issued_after_gate_refactor(monkeypatch):
    c = _core_client(monkeypatch)
    r = c.post("/api/plugins/_browser/ticket",
               json={"key": "chipricing", "url": "https://orderentry.chiohd.com/"})
    assert r.status_code == 200 and r.json()["ticket"]
    # Issuing a browser capability is recorded with the URL it was scoped to.
    assert "plugin.browser_ticket_issued" in _audit_actions(c)


def test_blocked_ticket_records_nothing(monkeypatch):
    """A refused request changed nothing, so it must not appear as an action."""
    c = _core_client(monkeypatch)
    r = c.post("/api/plugins/_browser/ticket",
               json={"key": "chipricing", "url": "https://evil.example.com/"})
    assert r.status_code == 400
    assert _audit_actions(c) == []
