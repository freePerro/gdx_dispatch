"""Slice outlook-s11 — verify GET/DELETE /api/oauth/outlook/account."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gdx_dispatch.core.database import get_db, get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.outlook_oauth import (
    get_db_for_oauth_start,
    get_db_for_oauth_callback,
    get_user_for_oauth_start,
    router as oauth_router,
)


TID = uuid4()
UID = uuid4()


def _user():
    return {"user_id": str(UID), "tenant_id": str(TID), "role": "tech"}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    app = FastAPI()
    app.include_router(oauth_router)
    cdb = MagicMock()
    tdb = MagicMock()
    app.dependency_overrides[get_user_for_oauth_start] = _user
    app.dependency_overrides[get_db_for_oauth_start] = lambda: cdb
    app.dependency_overrides[get_db_for_oauth_callback] = lambda: tdb
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = lambda: cdb
    app.dependency_overrides[get_db] = lambda: tdb
    app.dependency_overrides[require_module("email")] = lambda: None
    return TestClient(app, follow_redirects=False), cdb, tdb


def test_get_account_returns_disconnected_when_no_row(app):
    client, _, tdb = app
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    r = client.get("/api/oauth/outlook/account")
    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    body = r.json()
    assert body["connected"] is False
    assert body["upn"] is None


def test_get_account_returns_disconnected_when_tokens_cleared(app):
    client, _, tdb = app
    account = MagicMock()
    account.access_token_enc = None
    tdb.query.return_value.filter.return_value.one_or_none.return_value = account
    r = client.get("/api/oauth/outlook/account")
    assert r.json()["connected"] is False


def test_get_account_returns_metadata_when_connected(app):
    client, _, tdb = app
    account = MagicMock()
    account.access_token_enc = "fernet"
    account.upn = "doug@gdx"
    account.display_name = "Doug B"
    account.connected_at = datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc)
    account.last_sync_at = None
    account.last_error = None
    tdb.query.return_value.filter.return_value.one_or_none.return_value = account
    r = client.get("/api/oauth/outlook/account")
    body = r.json()
    assert body["connected"] is True
    assert body["upn"] == "doug@gdx"
    assert body["display_name"] == "Doug B"
    assert body["connected_at"].startswith("2026-04-27T10:00:00")


def test_get_account_never_returns_token_fields(app):
    """Compliance: the endpoint must NEVER expose access_token / refresh_token."""
    client, _, tdb = app
    account = MagicMock()
    account.access_token_enc = "fernet-very-secret"
    account.refresh_token_enc = "fernet-also-secret"
    account.upn = "u"
    account.display_name = None
    account.connected_at = None
    account.last_sync_at = None
    account.last_error = None
    tdb.query.return_value.filter.return_value.one_or_none.return_value = account
    r = client.get("/api/oauth/outlook/account")
    assert "fernet" not in r.text
    assert "access_token" not in r.text
    assert "refresh_token" not in r.text


def test_delete_calls_clear_user_tokens(app):
    client, _, tdb = app
    with patch("gdx_dispatch.routers.outlook_oauth.key_storage.clear_user_tokens") as clear:
        r = client.delete("/api/oauth/outlook/account")
    assert r.status_code == 204
    clear.assert_called_once()


def test_delete_is_idempotent(app):
    """Disconnecting twice doesn't error."""
    client, _, tdb = app
    with patch("gdx_dispatch.routers.outlook_oauth.key_storage.clear_user_tokens"):
        r1 = client.delete("/api/oauth/outlook/account")
        r2 = client.delete("/api/oauth/outlook/account")
    assert r1.status_code == 204
    assert r2.status_code == 204
