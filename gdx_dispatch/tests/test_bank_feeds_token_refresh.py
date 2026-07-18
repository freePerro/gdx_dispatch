"""Bank feeds token refresh — rotation persistence, failure classification,
fast path, host-mismatch guard. SQLite falls through the advisory-lock path
(dialect check) so these run unlocked, matching the QB precedent."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from gdx_dispatch.modules.bank_feeds import oauth
from gdx_dispatch.modules.bank_feeds.models import (
    AUTH_DISCONNECTED,
    AUTH_HEALTHY,
    AUTH_NEEDS_RECONNECT,
    AUTH_REFRESH_FAILED,
    BannoConnection,
    BannoInstitution,
)

FI_HOST = "digital.garden-fi.com"
TOKEN_URL = f"https://{FI_HOST}/a/consumer/api/v0/oidc/token"


def _setup(db, *, expires_in_s: int, auth_state: str = AUTH_HEALTHY):
    inst = BannoInstitution(
        fi_host=FI_HOST, display_label="Garden", client_id="cid",
        client_secret_enc=oauth._encrypt("secret"),
    )
    db.add(inst)
    db.commit()
    conn = BannoConnection(
        institution_id=inst.id, fi_host=FI_HOST, banno_user_id="sub-1",
        access_token_enc=oauth._encrypt("old-access"),
        refresh_token_enc=oauth._encrypt("old-refresh"),
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in_s),
        auth_state=auth_state,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return inst, conn


def test_fast_path_no_refresh(tenant_db):
    _, conn = _setup(tenant_db, expires_in_s=600)
    with respx.mock:  # NO routes mocked — any HTTP call would error
        token = oauth.get_valid_access_token(tenant_db, conn.id)
    assert token == "old-access"


@respx.mock
def test_expiring_token_refreshes_and_persists_rotation(respx_mock, tenant_db):
    _, conn = _setup(tenant_db, expires_in_s=30)  # inside the 2-min margin
    respx_mock.post(TOKEN_URL).mock(return_value=Response(200, json={
        "access_token": "new-access", "refresh_token": "rotated-refresh", "expires_in": 600,
    }))
    token = oauth.get_valid_access_token(tenant_db, conn.id)
    assert token == "new-access"
    row = tenant_db.execute(select(BannoConnection)).scalar_one()
    assert oauth._decrypt(row.refresh_token_enc) == "rotated-refresh"  # rotation persisted
    assert row.auth_state == AUTH_HEALTHY
    expires = row.access_token_expires_at
    if expires.tzinfo is None:  # SQLite returns naive datetimes
        expires = expires.replace(tzinfo=timezone.utc)
    assert expires > datetime.now(timezone.utc) + timedelta(minutes=5)


@respx.mock
def test_stale_token_forces_refresh_inside_expiry_window(respx_mock, tenant_db):
    _, conn = _setup(tenant_db, expires_in_s=600)  # comfortably valid
    respx_mock.post(TOKEN_URL).mock(return_value=Response(200, json={
        "access_token": "new-access", "refresh_token": "rotated", "expires_in": 600,
    }))
    # The caller got a 401 with "old-access" — refresh despite the window.
    token = oauth.get_valid_access_token(tenant_db, conn.id, stale_token="old-access")
    assert token == "new-access"


@respx.mock
def test_invalid_grant_marks_needs_reconnect_and_raises(respx_mock, tenant_db):
    _, conn = _setup(tenant_db, expires_in_s=30)
    respx_mock.post(TOKEN_URL).mock(return_value=Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(oauth.BankFeedsRefreshError):
        oauth.get_valid_access_token(tenant_db, conn.id)
    row = tenant_db.execute(select(BannoConnection)).scalar_one()
    assert row.auth_state == AUTH_NEEDS_RECONNECT
    assert oauth.connection_healthy(row) is False


@respx.mock
def test_network_error_marks_refresh_failed(respx_mock, tenant_db):
    import httpx

    _, conn = _setup(tenant_db, expires_in_s=30)
    respx_mock.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(oauth.BankFeedsRefreshError):
        oauth.get_valid_access_token(tenant_db, conn.id)
    row = tenant_db.execute(select(BannoConnection)).scalar_one()
    assert row.auth_state == AUTH_REFRESH_FAILED  # transient — retried next run


def test_host_mismatch_marks_needs_reconnect(tenant_db):
    inst, conn = _setup(tenant_db, expires_in_s=600)
    inst.fi_host = "digital.other-bank.example.com"
    tenant_db.commit()
    with pytest.raises(oauth.BankFeedsAuthError, match="host changed"):
        oauth.get_valid_access_token(tenant_db, conn.id)
    row = tenant_db.execute(select(BannoConnection)).scalar_one()
    assert row.auth_state == AUTH_NEEDS_RECONNECT


def test_disconnected_connection_refuses(tenant_db):
    _, conn = _setup(tenant_db, expires_in_s=600, auth_state=AUTH_DISCONNECTED)
    conn.refresh_token_enc = None
    tenant_db.commit()
    with pytest.raises(oauth.BankFeedsAuthError, match="disconnected"):
        oauth.get_valid_access_token(tenant_db, conn.id)


def test_soft_disconnect_keeps_rows_and_nulls_tokens(tenant_db):
    inst, conn = _setup(tenant_db, expires_in_s=600)
    count = oauth.soft_disconnect(tenant_db, inst.id)
    assert count == 1
    row = tenant_db.execute(select(BannoConnection)).scalar_one()
    assert row.id == conn.id  # row KEPT — reconnect resumes cursors
    assert row.access_token_enc is None
    assert row.refresh_token_enc is None
    assert row.auth_state == AUTH_DISCONNECTED
