"""Slice outlook-s7 — verify the OAuth start endpoint."""
from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.database import get_db, get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.outlook_oauth import (
    get_db_for_oauth_start,
    get_user_for_oauth_start,
    router as oauth_router,
)


TID = uuid4()
UID = uuid4()


def _user():
    return {"user_id": str(UID), "tenant_id": str(TID), "role": "technician"}


def _settings_configured():
    s = MagicMock()
    s.outlook_client_id = "abc-client-id"
    s.outlook_microsoft_tenant_id = "ms-tenant-guid-1234"
    return s


def _tenant():
    t = MagicMock()
    t.slug = "gdx"
    return t


@pytest.fixture
def app_for_start(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    monkeypatch.setenv("TENANT_BASE_DOMAIN", "example.com")

    app = FastAPI()
    app.include_router(oauth_router)
    db = MagicMock()
    db.get.side_effect = lambda model, _id: (
        _settings_configured() if model.__name__ == "TenantSettings" else _tenant()
    )
    app.dependency_overrides[get_user_for_oauth_start] = _user
    app.dependency_overrides[get_db_for_oauth_start] = lambda: db
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_db] = lambda: MagicMock()
    # lru_cache makes require_module("email") return the same dependency instance
    app.dependency_overrides[require_module("email")] = lambda: None
    return TestClient(app, follow_redirects=False), db


def test_start_returns_authorize_url(app_for_start):
    """SPA POSTs (with Bearer header) and gets back the Microsoft URL.

    The SPA can't use a top-level GET navigation because Authorization:
    Bearer is stored in sessionStorage, not cookies. So /start is a JSON
    POST that returns the URL the SPA navigates to next.
    """
    client, _ = app_for_start
    r = client.post("/api/oauth/outlook/start")
    assert r.status_code == 200
    body = r.json()
    url = body["authorize_url"]
    assert "login.microsoftonline.com/ms-tenant-guid-1234/oauth2/v2.0/authorize" in url
    assert "client_id=abc-client-id" in url
    assert "response_type=code" in url
    assert "Mail.Read" in url
    assert "offline_access" in url
    assert "state=" in url
    assert "redirect_uri=https%3A%2F%2Fgdx.example.com%2Fapi%2Foauth%2Foutlook%2Fcallback" in url


def test_start_400_when_no_client_configured(app_for_start):
    client, db = app_for_start
    s = MagicMock()
    s.outlook_client_id = None
    s.outlook_microsoft_tenant_id = None
    db.get.side_effect = lambda model, _id: (
        s if model.__name__ == "TenantSettings" else _tenant()
    )
    r = client.post("/api/oauth/outlook/start")
    assert r.status_code == 400
    assert "not configured" in r.text.lower()


def test_start_400_when_no_microsoft_tenant_id(app_for_start):
    client, db = app_for_start
    s = MagicMock()
    s.outlook_client_id = "abc"
    s.outlook_microsoft_tenant_id = None
    db.get.side_effect = lambda model, _id: (
        s if model.__name__ == "TenantSettings" else _tenant()
    )
    r = client.post("/api/oauth/outlook/start")
    assert r.status_code == 400


def test_state_round_trips_back_to_user_and_tenant(app_for_start):
    client, _ = app_for_start
    r = client.post("/api/oauth/outlook/start")
    from urllib.parse import parse_qs, urlparse
    url = r.json()["authorize_url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    from gdx_dispatch.routers.outlook_oauth import _state_signer
    payload = _state_signer().loads(state)
    assert payload["user_id"] == str(UID)
    assert payload["tenant_id"] == str(TID)


def test_state_signing_secret_required(app_for_start, monkeypatch):
    """All three signing-secret env vars unset → 500."""
    client, _ = app_for_start
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("STATE_SIGNING_KEY", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    r = client.post("/api/oauth/outlook/start")
    assert r.status_code == 500
    assert "signing" in r.text.lower()


def test_secret_key_fallback_works(app_for_start, monkeypatch):
    """Production (2026-04-28) runs RS256 with JWT_SECRET unset; SECRET_KEY
    is the fallback signing material. Verify /start succeeds when only
    SECRET_KEY is set."""
    client, _ = app_for_start
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("STATE_SIGNING_KEY", raising=False)
    monkeypatch.setenv("SECRET_KEY", "y" * 35)  # mirrors prod compose value len
    r = client.post("/api/oauth/outlook/start")
    assert r.status_code == 200
    assert "login.microsoftonline.com" in r.json()["authorize_url"]
