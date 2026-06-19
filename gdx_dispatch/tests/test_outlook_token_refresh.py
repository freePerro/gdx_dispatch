"""Slice outlook-s9 — verify refresh + 401-retry."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import respx
from httpx import Response

from gdx_dispatch.modules.outlook.token_refresh import (
    OutlookReconnectRequired,
    refresh_user_tokens,
    with_outlook_client,
)


TID = uuid4()
UID = uuid4()


def _control_db_with_settings():
    db = MagicMock()
    db.info = {"tenant_id": TID}
    s = MagicMock()
    s.outlook_client_id = "abc"
    s.outlook_microsoft_tenant_id = "ms-tid"
    db.get.return_value = s
    return db


def _tenant_db_with_account(refresh="ref-token", access="acc-token", expires_in_s=3600):
    db = MagicMock()
    account = MagicMock()
    account.refresh_token_enc = "fernet-ciphertext"
    account.user_id = UID
    db.query.return_value.filter.return_value.one_or_none.return_value = account
    return db, account


@respx.mock
def test_refresh_user_tokens_happy_path():
    cdb = _control_db_with_settings()
    tdb, account = _tenant_db_with_account()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=3600)

    respx.post(
        "https://login.microsoftonline.com/ms-tid/oauth2/v2.0/token"
    ).mock(return_value=Response(200, json={
        "access_token": "new-acc", "refresh_token": "new-ref",
        "expires_in": 3600, "scope": "Mail.Read offline_access",
    }))

    with patch("gdx_dispatch.modules.outlook.token_refresh.key_storage.get_user_tokens",
               return_value=("acc-token", "ref-token", expires_at)), \
         patch("gdx_dispatch.modules.outlook.token_refresh.key_storage.get_client_secret",
               return_value="secret"), \
         patch("gdx_dispatch.modules.outlook.token_refresh.key_storage.set_user_tokens") as set_tokens:
        out = refresh_user_tokens(cdb, tdb, UID)

    assert out == "new-acc"
    set_tokens.assert_called_once()
    assert set_tokens.call_args.kwargs["access_token"] == "new-acc"
    assert set_tokens.call_args.kwargs["refresh_token"] == "new-ref"


@respx.mock
def test_refresh_failure_writes_last_error_and_raises():
    cdb = _control_db_with_settings()
    tdb, account = _tenant_db_with_account()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=3600)

    respx.post(
        "https://login.microsoftonline.com/ms-tid/oauth2/v2.0/token"
    ).mock(return_value=Response(400, json={"error": "invalid_grant"}))

    with patch("gdx_dispatch.modules.outlook.token_refresh.key_storage.get_user_tokens",
               return_value=("acc", "ref", expires_at)), \
         patch("gdx_dispatch.modules.outlook.token_refresh.key_storage.get_client_secret",
               return_value="secret"), pytest.raises(OutlookReconnectRequired):
        refresh_user_tokens(cdb, tdb, UID)
    assert account.last_error is not None and "invalid_grant" in account.last_error


def test_refresh_no_account_raises_reconnect():
    cdb = _control_db_with_settings()
    tdb = MagicMock()
    tdb.query.return_value.filter.return_value.one_or_none.return_value = None
    with pytest.raises(OutlookReconnectRequired, match="no refresh token"):
        refresh_user_tokens(cdb, tdb, UID)


def test_with_outlook_client_proactively_refreshes_when_near_expiry():
    cdb = _control_db_with_settings()
    tdb, _ = _tenant_db_with_account()
    near_expiry = datetime.now(timezone.utc) + timedelta(seconds=60)  # < 5min threshold

    with patch("gdx_dispatch.modules.outlook.token_refresh.key_storage.get_user_tokens",
               return_value=("old", "ref", near_expiry)), \
         patch("gdx_dispatch.modules.outlook.token_refresh.refresh_user_tokens",
               return_value="fresh-acc") as refresh_call:
        with with_outlook_client(cdb, tdb, UID, TID) as gc:
            assert gc._access_token == "fresh-acc"
        refresh_call.assert_called_once()


def test_with_outlook_client_skips_refresh_when_token_fresh():
    cdb = _control_db_with_settings()
    tdb, _ = _tenant_db_with_account()
    fresh = datetime.now(timezone.utc) + timedelta(hours=1)

    with patch("gdx_dispatch.modules.outlook.token_refresh.key_storage.get_user_tokens",
               return_value=("acc", "ref", fresh)), \
         patch("gdx_dispatch.modules.outlook.token_refresh.refresh_user_tokens") as refresh_call:
        with with_outlook_client(cdb, tdb, UID, TID) as gc:
            assert gc._access_token == "acc"
        refresh_call.assert_not_called()


def test_with_outlook_client_no_tokens_raises_reconnect():
    cdb = _control_db_with_settings()
    tdb, _ = _tenant_db_with_account()
    with patch("gdx_dispatch.modules.outlook.token_refresh.key_storage.get_user_tokens",
               return_value=None), pytest.raises(OutlookReconnectRequired, match="not connected"):
        with with_outlook_client(cdb, tdb, UID, TID):
            pass
