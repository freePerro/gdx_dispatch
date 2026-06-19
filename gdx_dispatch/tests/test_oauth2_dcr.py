"""Sprint MCP-Streamable-HTTP S5 — RFC 7591 Dynamic Client Registration.

claude.ai's MCP connector signup walks:

  1. GET /.well-known/oauth-protected-resource          (S3)
  2. GET /.well-known/oauth-authorization-server        (S3, advertises
     `registration_endpoint`)
  3. POST /oauth/register                               (this slice)
  4. GET /oauth/authorize ... using client_id           (S4 path)
  5. POST /oauth/token ... mints tenant-bound JWT       (S4 path)

S5's job is step 3: a tenant-scoped DCR endpoint that returns 201 with
fresh client credentials, and the resulting client_id is usable for
the rest of the flow.
"""
from __future__ import annotations

import secrets
import uuid
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import oauth2_grants
from gdx_dispatch.core.oauth2_grants import compute_s256_challenge
from gdx_dispatch.core.tenant import TenantMiddleware
from gdx_dispatch.models.platform_ss20_additions import (
    DevPortalBase,
    OAuthDynamicClient,
)
from gdx_dispatch.routers.auth import oauth2 as oauth2_router_mod


GDX_HOST = "gdx.example.com"
ACME_HOST = "acme.example.com"
GDX_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
ACME_UUID = uuid.UUID("22222222-2222-2222-2222-222222222222")


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    DevPortalBase.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def SessionLocal(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _fake_tenant(slug: str | None) -> dict | None:
    if slug == "gdx":
        return {"id": GDX_UUID, "slug": "gdx", "db_url": "sqlite:///:memory:",
                "subscription_status": "active", "db_provisioned": True,
                "trial_ends_at": None}
    if slug == "acme":
        return {"id": ACME_UUID, "slug": "acme", "db_url": "sqlite:///:memory:",
                "subscription_status": "active", "db_provisioned": True,
                "trial_ends_at": None}
    return None


class _Sess:
    def close(self):
        pass


@pytest.fixture()
def client(SessionLocal):
    app = FastAPI()
    app.add_middleware(TenantMiddleware, control_session_factory=_Sess)
    app.include_router(oauth2_router_mod.router)

    def _get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[oauth2_router_mod.get_db] = _get_db
    code_redis = fakeredis.FakeRedis(decode_responses=True)
    token_redis = fakeredis.FakeRedis(decode_responses=True)
    oauth2_grants.set_code_store_for_tests(oauth2_grants._RedisCodeStore(code_redis))
    oauth2_router_mod.set_token_store_for_tests(oauth2_router_mod._RedisTokenStore(token_redis))

    def _resolver(db, *, slug=None, tenant_id=None):
        return _fake_tenant(slug)

    _single = {"id": str(GDX_UUID), "slug": "gdx", "db_url": "sqlite:///:memory:",
               "subscription_status": "active"}

    with patch("gdx_dispatch.core.tenant._lookup_tenant", side_effect=_resolver), \
         patch("gdx_dispatch.core.tenant.single_tenant", return_value=_single):
        with TestClient(app) as c:
            yield c

    oauth2_grants.set_code_store_for_tests(None)
    oauth2_router_mod.set_token_store_for_tests(None)


# ── happy-path RFC 7591 shape ───────────────────────────────────────────────


def test_register_returns_201_with_required_fields(client):
    r = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://client.example/cb"],
              "client_name": "claude.ai (test)"},
        headers={"Host": GDX_HOST},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # RFC 7591 §3.2.1 required response fields.
    assert body["client_id"].startswith("dcr_")
    assert "client_secret" in body  # default auth_method = client_secret_basic
    assert isinstance(body["client_id_issued_at"], int)
    assert body["client_secret_expires_at"] == 0
    # Echo of registered metadata.
    assert body["redirect_uris"] == ["https://client.example/cb"]
    assert body["grant_types"] == ["authorization_code", "refresh_token"]
    assert body["response_types"] == ["code"]
    assert body["client_name"] == "claude.ai (test)"


def test_register_persists_tenant_binding(client, SessionLocal):
    r = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://x/cb"]},
        headers={"Host": GDX_HOST},
    )
    body = r.json()
    cid = body["client_id"]
    db = SessionLocal()
    try:
        row = db.query(OAuthDynamicClient).filter_by(client_id=cid).first()
        assert row is not None
        assert row.tenant_id == str(GDX_UUID)
        assert row.client_secret_hash is not None
        assert row.secret_prefix is not None
        assert row.client_secret_hash != body["client_secret"]  # hashed, not plaintext
    finally:
        db.close()


def test_register_under_any_host_binds_to_the_single_tenant(client, SessionLocal):
    """Single-tenant collapse: host no longer selects the tenant.

    Pre-collapse, ``ACME_HOST`` bound the DCR client to the ACME tenant via
    TenantMiddleware's subdomain resolution. GDXDispatch serves exactly one
    tenant, so every host — including what used to be a different tenant's
    subdomain — pins to the single tenant (GDX_UUID).
    """
    r = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://x/cb"]},
        headers={"Host": ACME_HOST},
    )
    cid = r.json()["client_id"]
    db = SessionLocal()
    try:
        row = db.query(OAuthDynamicClient).filter_by(client_id=cid).first()
        assert row.tenant_id == str(GDX_UUID)
    finally:
        db.close()


def test_register_public_client_with_auth_method_none(client):
    """token_endpoint_auth_method=none → public client (no secret)."""
    r = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://x/cb"], "token_endpoint_auth_method": "none"},
        headers={"Host": GDX_HOST},
    )
    assert r.status_code == 201
    body = r.json()
    assert "client_secret" not in body
    assert body["token_endpoint_auth_method"] == "none"


# ── validation ──────────────────────────────────────────────────────────────


def test_register_without_redirect_uris_returns_400(client):
    r = client.post("/oauth/register", json={}, headers={"Host": GDX_HOST})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


def test_register_with_empty_redirect_uris_returns_400(client):
    r = client.post("/oauth/register", json={"redirect_uris": []},
                    headers={"Host": GDX_HOST})
    assert r.status_code == 400


def test_register_rejects_disallowed_grant_type(client):
    r = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://x/cb"], "grant_types": ["password"]},
        headers={"Host": GDX_HOST},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client_metadata"


def test_register_rejects_disallowed_response_type(client):
    r = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://x/cb"], "response_types": ["token"]},
        headers={"Host": GDX_HOST},
    )
    assert r.status_code == 400


def test_register_rejects_invalid_token_endpoint_auth_method(client):
    r = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://x/cb"],
              "token_endpoint_auth_method": "client_secret_jwt"},
        headers={"Host": GDX_HOST},
    )
    assert r.status_code == 400


def test_register_unknown_host_still_binds_to_the_single_tenant(client, SessionLocal):
    """Single-tenant collapse: an unrecognized host no longer 404/400s.

    Pre-collapse, TenantMiddleware rejected an unknown subdomain with
    "Unknown tenant" before the route ran, so DCR could not tenant-bind. With
    one pinned tenant there is no such thing as an unknown host — registration
    succeeds and binds to the single tenant.
    """
    r = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://x/cb"]},
        headers={"Host": "stranger.example.com"},
    )
    assert r.status_code == 201, r.text
    cid = r.json()["client_id"]
    db = SessionLocal()
    try:
        row = db.query(OAuthDynamicClient).filter_by(client_id=cid).first()
        assert row.tenant_id == str(GDX_UUID)
    finally:
        db.close()


# ── end-to-end: registered client can authorize ─────────────────────────────


def test_dcr_client_can_complete_authorize_flow(client):
    """Round-trip: /oauth/register → /oauth/authorize with the issued
    client_id → 302 with auth code. Confirms `_load_client` finds the
    DCR row alongside the legacy DeveloperApp lookup."""
    reg = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://client.example/cb"]},
        headers={"Host": GDX_HOST},
    )
    cid = reg.json()["client_id"]

    verifier = secrets.token_urlsafe(64)[:80]
    challenge = compute_s256_challenge(verifier)
    r = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": "https://client.example/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "mcp:invoke",
            "state": "z",
            "subject_id": "user-1",
        },
        headers={"Host": GDX_HOST},
        follow_redirects=False,
    )
    assert r.status_code in (302, 307), r.text
    qs = parse_qs(urlparse(r.headers["location"]).query)
    assert "code" in qs
