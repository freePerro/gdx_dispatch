"""Bank feeds OAuth — signed single-use state, callback verification, and
the connection lifecycle. Hostnames are Garden/RFC-2606 only."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select

from gdx_dispatch.core.database import get_db
from gdx_dispatch.modules.bank_feeds import oauth
from gdx_dispatch.modules.bank_feeds.models import (
    AUTH_DISCONNECTED,
    AUTH_HEALTHY,
    BannoConnection,
    BannoInstitution,
)
from gdx_dispatch.modules.bank_feeds.router import oauth_callback
from gdx_dispatch.modules.bank_feeds.router import router as bank_feeds_router
from gdx_dispatch.tests.fixtures.keypairs import test_app_keypair  # noqa: F401

FI_HOST = "digital.garden-fi.com"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
SUB = "9f27a1f0-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _oauth_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    monkeypatch.setenv("GDX_BASE_URL", "https://app.example.com")
    # Fresh caches per test — discovery cache + local nonce store.
    oauth._discovery_cache.clear()
    oauth._local_nonces.clear()


def _make_institution(db, fi_host: str = FI_HOST) -> BannoInstitution:
    inst = BannoInstitution(
        fi_host=fi_host,
        display_label="Garden Test FI",
        client_id=CLIENT_ID,
        client_secret_enc=oauth._encrypt(CLIENT_SECRET),
        secret_set_at=datetime.now(timezone.utc),
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


def _discovery_doc(fi_host: str = FI_HOST) -> dict:
    return {
        "issuer": f"https://{fi_host}",
        "authorization_endpoint": f"https://{fi_host}/oauth/authorize",
        "jwks_uri": f"https://{fi_host}/oauth/jwks",
        "token_endpoint": f"https://{fi_host}/evil/ignored",  # pinned path wins
    }


def _jwk_for(keypair) -> dict:
    from cryptography.hazmat.primitives import serialization

    public_key = serialization.load_pem_public_key(keypair["public_pem"].encode())
    jwk = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    jwk["kid"] = keypair["kid"]
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return jwk


def _mint_id_token(keypair, *, nonce: str, sub: str = SUB, fi_host: str = FI_HOST, aud: str = CLIENT_ID) -> str:
    now = datetime.now(timezone.utc)
    return pyjwt.encode(
        {
            "iss": f"https://{fi_host}",
            "aud": aud,
            "sub": sub,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "nonce": nonce,
        },
        keypair["private_pem"],
        algorithm="RS256",
        headers={"kid": keypair["kid"]},
    )


def _mock_banno(respx_mock, keypair, *, nonce: str, sub: str = SUB, fi_host: str = FI_HOST):
    respx_mock.get(f"https://{fi_host}/.well-known/openid-configuration").mock(
        return_value=Response(200, json=_discovery_doc(fi_host))
    )
    respx_mock.get(f"https://{fi_host}/oauth/jwks").mock(
        return_value=Response(200, json={"keys": [_jwk_for(keypair)]})
    )
    token_route = respx_mock.post(f"https://{fi_host}/a/consumer/api/v0/oidc/token").mock(
        return_value=Response(200, json={
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "expires_in": 600,
            "id_token": _mint_id_token(keypair, nonce=nonce, sub=sub, fi_host=fi_host),
            "token_type": "Bearer",
        })
    )
    return token_route


TENANT_ID = "11111111-1111-1111-1111-111111111111"


def _grant_module(db, tenant_id: str = TENANT_ID, module_key: str = "bank_feeds") -> None:
    from uuid import uuid4 as _uuid4

    from sqlalchemy import text as _text

    from gdx_dispatch.core.modules import _ensure_company_module_grants_table

    _ensure_company_module_grants_table(db)
    db.execute(
        _text(
            "INSERT INTO company_module_grants (id, company_id, module_key, granted_at, created_at) "
            "VALUES (:id, :cid, :key, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"id": str(_uuid4()), "cid": tenant_id, "key": module_key},
    )
    db.commit()


@pytest.fixture
def callback_app(tenant_db):
    _grant_module(tenant_db)
    app = FastAPI()
    app.include_router(bank_feeds_router)
    app.dependency_overrides[get_db] = lambda: tenant_db
    return TestClient(app, raise_server_exceptions=False)


# ── state signing + nonce ──────────────────────────────────────────────


def test_state_round_trip_and_nonce_single_use():
    state, nonce = oauth.make_state(user_id="u1", tenant_id=TENANT_ID, institution_id="i1")
    payload = oauth.load_state(state)
    assert payload["institution_id"] == "i1"
    assert payload["nonce"] == nonce
    assert oauth.consume_nonce(nonce) is True
    assert oauth.consume_nonce(nonce) is False  # single use


def test_state_tampered_rejected():
    state, _ = oauth.make_state(user_id="u1", tenant_id=TENANT_ID, institution_id="i1")
    with pytest.raises(oauth.BankFeedsAuthError):
        oauth.load_state(state[:-4] + "AAAA")
    with pytest.raises(oauth.BankFeedsAuthError):
        oauth.load_state("garbage")


def test_fi_host_validation_rejects_urls_and_ips():
    for bad in ("https://h.example.com", "h.example.com/path", "h.example.com:8080",
                "10.0.0.1", "user@h.example.com", ""):
        with pytest.raises(Exception):
            oauth.validate_fi_host(bad)
    assert oauth.validate_fi_host("Digital.Garden-FI.com") == "digital.garden-fi.com"


# ── discovery hardening ────────────────────────────────────────────────


@respx.mock
def test_discovery_rejects_offhost_jwks(respx_mock):
    doc = _discovery_doc()
    doc["jwks_uri"] = "https://attacker.example.com/jwks"
    respx_mock.get(f"https://{FI_HOST}/.well-known/openid-configuration").mock(
        return_value=Response(200, json=doc)
    )
    with pytest.raises(oauth.BankFeedsAuthError, match="jwks_uri"):
        oauth.discover_oidc(FI_HOST)


# ── callback flow ──────────────────────────────────────────────────────


@respx.mock
def test_callback_happy_path(respx_mock, tenant_db, callback_app, test_app_keypair):
    inst = _make_institution(tenant_db)
    state, nonce = oauth.make_state(user_id="u1", tenant_id=TENANT_ID, institution_id=str(inst.id))
    _mock_banno(respx_mock, test_app_keypair, nonce=nonce)

    resp = callback_app.get(f"/api/bank-feeds/oauth/callback?code=abc&state={state}")
    assert resp.status_code == 200
    assert "bank_feeds_oauth_result" in resp.text
    assert '"status": "connected"' in resp.text.replace("\\", "")

    row = tenant_db.execute(select(BannoConnection)).scalar_one()
    assert row.banno_user_id == SUB
    assert row.auth_state == AUTH_HEALTHY
    assert row.fi_host == FI_HOST
    # Tokens never stored as the raw values when a Fernet key is present;
    # keyless test env stores plaintext — assert via decrypt round-trip.
    assert oauth._decrypt(row.access_token_enc) == "at-1"
    assert oauth._decrypt(row.refresh_token_enc) == "rt-1"


@respx.mock
def test_callback_state_replay_rejected(respx_mock, tenant_db, callback_app, test_app_keypair):
    inst = _make_institution(tenant_db)
    state, nonce = oauth.make_state(user_id="u1", tenant_id=TENANT_ID, institution_id=str(inst.id))
    token_route = _mock_banno(respx_mock, test_app_keypair, nonce=nonce)

    first = callback_app.get(f"/api/bank-feeds/oauth/callback?code=abc&state={state}")
    assert first.status_code == 200
    assert token_route.call_count == 1

    replay = callback_app.get(f"/api/bank-feeds/oauth/callback?code=abc&state={state}")
    assert replay.status_code == 400
    assert "already used" in replay.text
    assert token_route.call_count == 1  # no second exchange


@respx.mock
def test_callback_tampered_state_makes_zero_token_calls(respx_mock, tenant_db, callback_app, test_app_keypair):
    _make_institution(tenant_db)
    token_route = _mock_banno(respx_mock, test_app_keypair, nonce="n/a")
    resp = callback_app.get("/api/bank-feeds/oauth/callback?code=abc&state=forged")
    assert resp.status_code == 400
    assert token_route.call_count == 0
    assert tenant_db.execute(select(BannoConnection)).first() is None


@respx.mock
def test_callback_nonce_mismatch_rejected(respx_mock, tenant_db, callback_app, test_app_keypair):
    inst = _make_institution(tenant_db)
    state, _nonce = oauth.make_state(user_id="u1", tenant_id=TENANT_ID, institution_id=str(inst.id))
    # id_token minted for a DIFFERENT nonce.
    _mock_banno(respx_mock, test_app_keypair, nonce="some-other-nonce")

    resp = callback_app.get(f"/api/bank-feeds/oauth/callback?code=abc&state={state}")
    assert resp.status_code == 502
    assert tenant_db.execute(select(BannoConnection)).first() is None


@respx.mock
def test_callback_missing_id_token_rejected(respx_mock, tenant_db, callback_app, test_app_keypair):
    inst = _make_institution(tenant_db)
    state, nonce = oauth.make_state(user_id="u1", tenant_id=TENANT_ID, institution_id=str(inst.id))
    respx_mock.get(f"https://{FI_HOST}/.well-known/openid-configuration").mock(
        return_value=Response(200, json=_discovery_doc())
    )
    respx_mock.post(f"https://{FI_HOST}/a/consumer/api/v0/oidc/token").mock(
        return_value=Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 600})
    )
    resp = callback_app.get(f"/api/bank-feeds/oauth/callback?code=abc&state={state}")
    assert resp.status_code == 502
    assert tenant_db.execute(select(BannoConnection)).first() is None


@respx.mock
def test_callback_module_revoked_writes_nothing(respx_mock, tenant_db, test_app_keypair):
    """Module grant revoked mid-flow → friendly HTML, zero writes (S12).

    NOTE: an EMPTY grant table means "first boot" and seeds every module
    (single-tenant seeding), so revocation is simulated the way it really
    happens — other grants present, bank_feeds absent."""
    _grant_module(tenant_db, module_key="jobs")  # some grants exist; bank_feeds not among them
    app = FastAPI()
    app.include_router(bank_feeds_router)
    app.dependency_overrides[get_db] = lambda: tenant_db
    client = TestClient(app, raise_server_exceptions=False)

    inst = _make_institution(tenant_db)
    state, nonce = oauth.make_state(user_id="u1", tenant_id=TENANT_ID, institution_id=str(inst.id))
    token_route = _mock_banno(respx_mock, test_app_keypair, nonce=nonce)

    resp = client.get(f"/api/bank-feeds/oauth/callback?code=abc&state={state}")
    assert resp.status_code == 403
    assert "not enabled" in resp.text
    assert token_route.call_count == 0
    assert tenant_db.execute(select(BannoConnection)).first() is None


def test_callback_error_param_renders_friendly_html(callback_app, tenant_db):
    resp = callback_app.get("/api/bank-feeds/oauth/callback?error=access_denied")
    assert resp.status_code == 200
    assert "declined" in resp.text
    assert tenant_db.execute(select(BannoConnection)).first() is None


@respx.mock
def test_callback_different_active_sub_rejected(respx_mock, tenant_db, callback_app, test_app_keypair):
    inst = _make_institution(tenant_db)
    existing = BannoConnection(
        institution_id=inst.id, fi_host=FI_HOST, banno_user_id="other-sub",
        access_token_enc="x", refresh_token_enc="y", auth_state=AUTH_HEALTHY,
    )
    tenant_db.add(existing)
    tenant_db.commit()

    state, nonce = oauth.make_state(user_id="u1", tenant_id=TENANT_ID, institution_id=str(inst.id))
    _mock_banno(respx_mock, test_app_keypair, nonce=nonce)
    resp = callback_app.get(f"/api/bank-feeds/oauth/callback?code=abc&state={state}")
    assert resp.status_code == 409
    assert "another bank user" in resp.text
    subs = {r.banno_user_id for r in tenant_db.execute(select(BannoConnection)).scalars()}
    assert subs == {"other-sub"}


@respx.mock
def test_reconnect_same_sub_reuses_row(respx_mock, tenant_db, callback_app, test_app_keypair):
    """Soft-disconnected connection is REUSED on reconnect — cursors and
    account dedupe keys survive (audited plan B1)."""
    inst = _make_institution(tenant_db)
    old = BannoConnection(
        institution_id=inst.id, fi_host=FI_HOST, banno_user_id=SUB,
        auth_state=AUTH_DISCONNECTED,
    )
    tenant_db.add(old)
    tenant_db.commit()
    old_id = old.id

    state, nonce = oauth.make_state(user_id="u1", tenant_id=TENANT_ID, institution_id=str(inst.id))
    _mock_banno(respx_mock, test_app_keypair, nonce=nonce)
    resp = callback_app.get(f"/api/bank-feeds/oauth/callback?code=abc&state={state}")
    assert resp.status_code == 200

    rows = tenant_db.execute(select(BannoConnection)).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == old_id
    assert rows[0].auth_state == AUTH_HEALTHY
    assert oauth._decrypt(rows[0].refresh_token_enc) == "rt-1"
