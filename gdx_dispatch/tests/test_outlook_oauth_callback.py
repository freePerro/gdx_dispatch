"""Slice outlook-s8 — verify token exchange + state validation in /callback."""
from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from gdx_dispatch.core.database import get_db, get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.outlook_oauth import (
    _state_signer,
    get_db_for_oauth_start,
    get_db_for_oauth_callback,
    get_user_for_oauth_start,
    router as oauth_router,
)


TID = uuid4()
UID = uuid4()


def _user():
    return {"user_id": str(UID), "tenant_id": str(TID), "role": "technician"}


def _control_db():
    db = MagicMock()
    s = MagicMock()
    s.outlook_client_id = "abc-client-id"
    s.outlook_microsoft_tenant_id = "ms-tenant-guid"
    s.outlook_client_secret_enc = "fernet-ciphertext-placeholder"
    t = MagicMock()
    t.slug = "gdx"
    db.get.side_effect = lambda model, _id: s if model.__name__ == "TenantSettings" else t
    return db


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    monkeypatch.setenv("TENANT_BASE_DOMAIN", "example.com")

    app = FastAPI()
    app.include_router(oauth_router)
    cdb = _control_db()
    tdb = MagicMock()
    app.dependency_overrides[get_user_for_oauth_start] = _user
    app.dependency_overrides[get_db_for_oauth_start] = lambda: cdb
    app.dependency_overrides[get_db_for_oauth_callback] = lambda: tdb
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = lambda: cdb
    app.dependency_overrides[get_db] = lambda: tdb
    app.dependency_overrides[require_module("email")] = lambda: None
    client = TestClient(app, follow_redirects=False)
    return client, cdb, tdb


def _good_state():
    return _state_signer().dumps({"user_id": str(UID), "tenant_id": str(TID)})


@respx.mock
def test_callback_happy_path_redirects_to_settings_ok(app):
    client, _, tdb = app
    respx.post(
        "https://login.microsoftonline.com/ms-tenant-guid/oauth2/v2.0/token"
    ).mock(return_value=Response(200, json={
        "access_token": "acc1", "refresh_token": "ref1",
        "expires_in": 3600, "scope": "Mail.Read offline_access",
    }))
    respx.get("https://graph.microsoft.com/v1.0/me").mock(return_value=Response(
        200, json={"id": "ms-uid", "userPrincipalName": "doug@gdx",
                   "displayName": "Doug B"}
    ))

    with patch("gdx_dispatch.routers.outlook_oauth.key_storage.get_client_secret",
               return_value="real-secret-decrypted"), \
         patch("gdx_dispatch.routers.outlook_oauth.key_storage.set_user_tokens") as set_tokens:
        r = client.get(f"/api/oauth/outlook/callback?code=abc&state={_good_state()}")

    assert r.status_code == 302
    assert r.headers["location"].startswith("/settings?integration=outlook&status=ok")
    set_tokens.assert_called_once()
    kwargs = set_tokens.call_args.kwargs
    assert kwargs["access_token"] == "acc1"
    assert kwargs["refresh_token"] == "ref1"
    assert kwargs["upn"] == "doug@gdx"


def test_callback_with_microsoft_error_redirects_to_error(app):
    client, _, _ = app
    r = client.get("/api/oauth/outlook/callback?error=access_denied&error_description=user+cancelled")
    assert r.status_code == 302
    assert "status=error" in r.headers["location"]
    assert "detail=access_denied" in r.headers["location"]


def test_callback_missing_code_or_state_rejects(app):
    client, _, _ = app
    r = client.get("/api/oauth/outlook/callback")
    assert "status=error" in r.headers["location"]


def test_callback_invalid_state_rejects(app):
    client, _, _ = app
    r = client.get("/api/oauth/outlook/callback?code=abc&state=tampered")
    assert "detail=invalid_state" in r.headers["location"]


def test_callback_rejects_state_missing_user_id(app):
    """The state's signed payload IS the auth proof — if it's missing the
    user_id, we have no way to attribute the tokens. Reject."""
    client, _, _ = app
    bad_state = _state_signer().dumps({"tenant_id": str(TID)})  # no user_id
    r = client.get(f"/api/oauth/outlook/callback?code=abc&state={bad_state}")
    assert "detail=state_missing_ids" in r.headers["location"]


def test_callback_works_without_bearer_header(app):
    """Microsoft redirects the user's browser here — no Authorization
    header. The signed state alone must authenticate the request. Pre-fix
    (2026-04-28) this returned 401; post-fix the state is trusted."""
    client, _, _ = app
    # No Bearer header. Use a valid state — should NOT 401, should follow
    # the normal callback path. We mock the token exchange to fail so we
    # land on a known error redirect rather than the happy path.
    import respx
    from httpx import Response
    with respx.mock(assert_all_called=False) as router:
        router.post(
            "https://login.microsoftonline.com/ms-tenant-guid/oauth2/v2.0/token"
        ).mock(return_value=Response(400, json={"error": "invalid_grant"}))
        with patch("gdx_dispatch.routers.outlook_oauth.key_storage.get_client_secret",
                   return_value="real-secret"):
            r = client.get(f"/api/oauth/outlook/callback?code=abc&state={_good_state()}")
    assert r.status_code == 302
    assert "/settings?integration=outlook" in r.headers["location"]
    assert "401" not in r.headers["location"]


@respx.mock
def test_callback_token_exchange_failure_redirects_error(app):
    client, _, _ = app
    respx.post(
        "https://login.microsoftonline.com/ms-tenant-guid/oauth2/v2.0/token"
    ).mock(return_value=Response(400, json={"error": "invalid_grant"}))
    with patch("gdx_dispatch.routers.outlook_oauth.key_storage.get_client_secret",
               return_value="real-secret"):
        r = client.get(f"/api/oauth/outlook/callback?code=bad&state={_good_state()}")
    assert "detail=token_exchange_failed" in r.headers["location"]


def test_callback_redirects_only_to_relative_settings_url(app):
    """No open-redirect — every error path lands on /settings?... """
    client, _, _ = app
    r = client.get("/api/oauth/outlook/callback?error=foo")
    loc = r.headers["location"]
    assert loc.startswith("/settings?")
    assert "://" not in loc, "must not redirect to absolute URL — open-redirect risk"
