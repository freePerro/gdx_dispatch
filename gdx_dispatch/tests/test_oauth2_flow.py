"""
gdx_dispatch/tests/test_oauth2_flow.py — SS-21 OAuth2 authorization-code + PKCE tests.

Covers:
  - /oauth/authorize happy path with PKCE S256
  - /oauth/authorize rejects plain / non-S256 code_challenge_method
  - /oauth/authorize rejects unregistered redirect_uri
  - /oauth/token happy path: code redemption → access_token + refresh_token
  - /oauth/token rejects reused auth code (single-use per RFC 6749 §10.5)
  - /oauth/token rejects wrong PKCE verifier
  - /oauth/token rejects mismatched redirect_uri
  - /oauth/token rejects unknown client
  - /oauth/revoke invalidates the token (per RFC 7009)
  - /oauth/introspect returns active=true for valid token, false for revoked
"""
from __future__ import annotations

import secrets

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import oauth2_grants
from gdx_dispatch.core.oauth2_grants import (
    _RedisCodeStore,
    compute_s256_challenge,
    consume_authorization_code,
    mint_authorization_code,
)
from gdx_dispatch.models.platform_ss20_additions import (
    DeveloperAccount,
    DeveloperApp,
    DevPortalBase,
)
from gdx_dispatch.routers.auth import oauth2 as oauth2_router_mod


def _make_verifier_and_challenge() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:80]
    return verifier, compute_s256_challenge(verifier)


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    DevPortalBase.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def SessionLocal(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture()
def seeded_app(SessionLocal):
    """Create one DeveloperAccount + DeveloperApp with a known client_id."""
    db = SessionLocal()
    try:
        acct = DeveloperAccount(
            email="dev@ex.com",
            password_hash="x",
            tier="sandbox",
        )
        db.add(acct)
        db.flush()
        app_row = DeveloperApp(
            account_id=acct.id,
            name="Test App",
            client_id="client_abc",
            redirect_uri="https://client.example/cb",
            scopes="read:jobs write:jobs",
        )
        db.add(app_row)
        db.commit()
        return {
            "client_id": app_row.client_id,
            "redirect_uri": app_row.redirect_uri,
        }
    finally:
        db.close()


@pytest.fixture()
def client(SessionLocal, seeded_app):
    app = FastAPI()
    app.include_router(oauth2_router_mod.router)

    def _get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[oauth2_router_mod.get_db] = _get_db
    # Inject isolated FakeRedis-backed stores for this test. decode_responses=True
    # matches the prod client in oauth2_grants._get_redis / oauth2._get_redis.
    code_redis = fakeredis.FakeRedis(decode_responses=True)
    token_redis = fakeredis.FakeRedis(decode_responses=True)
    oauth2_grants.set_code_store_for_tests(
        oauth2_grants._RedisCodeStore(code_redis)
    )
    oauth2_router_mod.set_token_store_for_tests(
        oauth2_router_mod._RedisTokenStore(token_redis)
    )
    with TestClient(app) as c:
        yield c
    oauth2_grants.set_code_store_for_tests(None)
    oauth2_router_mod.set_token_store_for_tests(None)


# ---------------------------------------------------------------------------
# /oauth/authorize
# ---------------------------------------------------------------------------


def test_authorize_requires_s256(client, seeded_app):
    v, ch = _make_verifier_and_challenge()
    r = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": seeded_app["client_id"],
            "redirect_uri": seeded_app["redirect_uri"],
            "code_challenge": ch,
            "code_challenge_method": "plain",  # ← rejected
            "scope": "read:jobs",
            "state": "xyz",
            "subject_id": "user-1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "S256" in r.text or "code_challenge_method" in r.text


def test_authorize_rejects_bad_redirect(client, seeded_app):
    _, ch = _make_verifier_and_challenge()
    r = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": seeded_app["client_id"],
            "redirect_uri": "https://evil.example/cb",  # not registered
            "code_challenge": ch,
            "code_challenge_method": "S256",
            "scope": "read:jobs",
            "state": "xyz",
            "subject_id": "user-1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_authorize_issues_code_and_redirects(client, seeded_app):
    _, ch = _make_verifier_and_challenge()
    r = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": seeded_app["client_id"],
            "redirect_uri": seeded_app["redirect_uri"],
            "code_challenge": ch,
            "code_challenge_method": "S256",
            "scope": "read:jobs",
            "state": "xyz123",
            "subject_id": "user-1",
        },
        follow_redirects=False,
    )
    # 302 redirect to redirect_uri with ?code=...&state=...
    assert r.status_code in (302, 307)
    loc = r.headers["location"]
    assert loc.startswith(seeded_app["redirect_uri"])
    assert "code=" in loc
    assert "state=xyz123" in loc


# ---------------------------------------------------------------------------
# /oauth/token
# ---------------------------------------------------------------------------


def _authorize_and_extract_code(client, seeded_app, challenge):
    r = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": seeded_app["client_id"],
            "redirect_uri": seeded_app["redirect_uri"],
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "read:jobs",
            "state": "s",
            "subject_id": "user-1",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 307), r.text
    loc = r.headers["location"]
    # very simple parse
    qs = loc.split("?", 1)[1]
    params = dict(p.split("=", 1) for p in qs.split("&"))
    return params["code"]


def test_token_exchange_happy_path(client, seeded_app):
    verifier, challenge = _make_verifier_and_challenge()
    code = _authorize_and_extract_code(client, seeded_app, challenge)

    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": seeded_app["redirect_uri"],
            "client_id": seeded_app["client_id"],
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["expires_in"] > 0


def test_token_rejects_reused_code(client, seeded_app):
    verifier, challenge = _make_verifier_and_challenge()
    code = _authorize_and_extract_code(client, seeded_app, challenge)

    # First redemption succeeds
    r1 = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": seeded_app["redirect_uri"],
            "client_id": seeded_app["client_id"],
            "code_verifier": verifier,
        },
    )
    assert r1.status_code == 200

    # Second redemption is a replay — MUST fail
    r2 = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": seeded_app["redirect_uri"],
            "client_id": seeded_app["client_id"],
            "code_verifier": verifier,
        },
    )
    assert r2.status_code == 400
    assert r2.json()["error"] == "invalid_grant"


def test_token_rejects_wrong_pkce(client, seeded_app):
    _verifier, challenge = _make_verifier_and_challenge()
    code = _authorize_and_extract_code(client, seeded_app, challenge)

    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": seeded_app["redirect_uri"],
            "client_id": seeded_app["client_id"],
            "code_verifier": "not-the-real-verifier",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_token_rejects_redirect_mismatch(client, seeded_app):
    verifier, challenge = _make_verifier_and_challenge()
    code = _authorize_and_extract_code(client, seeded_app, challenge)

    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://other.example/cb",
            "client_id": seeded_app["client_id"],
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_token_rejects_unknown_client(client, seeded_app):
    verifier, challenge = _make_verifier_and_challenge()
    code = _authorize_and_extract_code(client, seeded_app, challenge)

    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": seeded_app["redirect_uri"],
            "client_id": "bogus-client",
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 400


def test_token_rejects_unsupported_grant(client, seeded_app):
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "password",
            "username": "a",
            "password": "b",
            "client_id": seeded_app["client_id"],
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_grant_type"


# ---------------------------------------------------------------------------
# /oauth/revoke + /oauth/introspect — RFC 7009 / 7662
# ---------------------------------------------------------------------------


def _issue_token(client, seeded_app):
    verifier, challenge = _make_verifier_and_challenge()
    code = _authorize_and_extract_code(client, seeded_app, challenge)
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": seeded_app["redirect_uri"],
            "client_id": seeded_app["client_id"],
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_introspect_active(client, seeded_app):
    tok = _issue_token(client, seeded_app)
    r = client.post(
        "/oauth/introspect",
        data={
            "token": tok["access_token"],
            "client_id": seeded_app["client_id"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is True
    assert body["client_id"] == seeded_app["client_id"]


def test_revoke_then_introspect_inactive(client, seeded_app):
    tok = _issue_token(client, seeded_app)
    r = client.post(
        "/oauth/revoke",
        data={
            "token": tok["access_token"],
            "client_id": seeded_app["client_id"],
        },
    )
    # RFC 7009: revocation always returns 200 (even for unknown tokens)
    assert r.status_code == 200

    r2 = client.post(
        "/oauth/introspect",
        data={
            "token": tok["access_token"],
            "client_id": seeded_app["client_id"],
        },
    )
    assert r2.status_code == 200
    assert r2.json()["active"] is False


# ---------------------------------------------------------------------------
# SS 0.9-h — Redis-backed code store: survives in-process "restart"
# ---------------------------------------------------------------------------


def test_oauth_store_survives_mock_restart():
    """Mint a code against one ``_RedisCodeStore`` instance, throw the store
    object away, rebuild a new ``_RedisCodeStore`` pointed at the SAME
    fakeredis server, and confirm ``consume_authorization_code`` still
    returns the record exactly once (and None on the replay).

    This is the load-bearing property of the Redis swap: auth codes survive
    app-restart within their 60s TTL window. The in-memory predecessor
    failed this — codes evaporated on process exit.
    """
    shared_redis = fakeredis.FakeRedis(decode_responses=True)

    # First "process" — mint the code.
    store_a = _RedisCodeStore(shared_redis)
    code = mint_authorization_code(
        client_id="cid",
        redirect_uri="https://c/cb",
        scope="read",
        code_challenge="ch",
        code_challenge_method="S256",
        store=store_a,
    )
    del store_a  # simulate process exit — the client object is gone

    # Second "process" — same Redis server, brand-new store instance.
    store_b = _RedisCodeStore(shared_redis)
    rec = consume_authorization_code(code, store=store_b)
    assert rec is not None
    assert rec.client_id == "cid"
    assert rec.redirect_uri == "https://c/cb"

    # Replay is rejected by GETDEL atomicity.
    assert consume_authorization_code(code, store=store_b) is None


def test_introspect_unknown_token_inactive(client, seeded_app):
    r = client.post(
        "/oauth/introspect",
        data={
            "token": "bogus-token",
            "client_id": seeded_app["client_id"],
        },
    )
    assert r.status_code == 200
    assert r.json()["active"] is False
