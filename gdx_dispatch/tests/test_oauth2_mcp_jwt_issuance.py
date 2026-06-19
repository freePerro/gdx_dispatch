"""Sprint MCP-Streamable-HTTP S4 — /oauth/authorize + /oauth/token issuance.

Confirms the OAuth flow under a tenant host:

* `/oauth/authorize` derives the tenant from `request.state.tenant["id"]`
  (set by TenantMiddleware) — caller-supplied `tenant_id` query param is
  ignored when the request was tenant-resolved.
* `/oauth/token` mints a tenant-bound JWT (not an opaque token) when
  the auth code carries a `resource` indicator pointing at `<host>/mcp`.
  The JWT's `iss`, `aud`, and `gdx_tid` claims match the request host
  and tenant — the same shape that `verify_mcp_bearer` enforces in the
  transport middleware.
"""
from __future__ import annotations

import secrets
import uuid
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import fakeredis
import jwt as pyjwt
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
    DeveloperAccount,
    DeveloperApp,
    DevPortalBase,
)
from gdx_dispatch.routers.auth import oauth2 as oauth2_router_mod


GDX_HOST = "gdx.example.com"
GDX_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
ACME_UUID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _verifier_and_challenge() -> tuple[str, str]:
    v = secrets.token_urlsafe(64)[:80]
    return v, compute_s256_challenge(v)


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


@pytest.fixture()
def seeded_app(SessionLocal):
    db = SessionLocal()
    try:
        acct = DeveloperAccount(email="dev@ex.com", password_hash="x", tier="sandbox")
        db.add(acct)
        db.flush()
        app_row = DeveloperApp(
            account_id=acct.id, name="MCP Test App",
            client_id="mcp-client", redirect_uri="https://client.example/cb",
            scopes="mcp:invoke",
        )
        db.add(app_row)
        db.commit()
        return {"client_id": app_row.client_id, "redirect_uri": app_row.redirect_uri}
    finally:
        db.close()


class _Sess:
    def close(self):
        pass


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


@pytest.fixture()
def client(SessionLocal, seeded_app):
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


def _authorize_under_tenant(c: TestClient, seeded_app, host: str,
                            challenge: str, *, malicious_tenant_id: str | None = None,
                            extra_params: dict | None = None) -> str:
    """Perform GET /oauth/authorize and return the issued auth code."""
    params = {
        "response_type": "code",
        "client_id": seeded_app["client_id"],
        "redirect_uri": seeded_app["redirect_uri"],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "mcp:invoke",
        "state": "x",
        "subject_id": "user-1",
    }
    if malicious_tenant_id:
        params["tenant_id"] = malicious_tenant_id
    if extra_params:
        params.update(extra_params)
    r = c.get("/oauth/authorize", params=params, headers={"Host": host},
              follow_redirects=False)
    assert r.status_code in (302, 307), r.text
    qs = parse_qs(urlparse(r.headers["location"]).query)
    return qs["code"][0]


def _redeem(c: TestClient, seeded_app, code: str, verifier: str, host: str,
            *, resource: str | None = None) -> dict:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": seeded_app["redirect_uri"],
        "client_id": seeded_app["client_id"],
        "code_verifier": verifier,
    }
    if resource is not None:
        form["resource"] = resource
    r = c.post("/oauth/token", data=form, headers={"Host": host})
    return r


# ── happy path: tenant-bound JWT issuance ───────────────────────────────────


def _read_secret() -> str:
    import os
    return os.environ["JWT_SECRET"]


def test_oauth_token_mints_jwt_with_tenant_bound_claims(client, seeded_app):
    v, ch = _verifier_and_challenge()
    code = _authorize_under_tenant(
        client, seeded_app, GDX_HOST, ch,
        extra_params={"resource": f"http://{GDX_HOST}/mcp"},
    )
    r = _redeem(client, seeded_app, code, v, GDX_HOST,
                resource=f"http://{GDX_HOST}/mcp")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "Bearer"
    # Three load-bearing claims.
    claims = pyjwt.decode(
        body["access_token"], _read_secret(), algorithms=["HS256"],
        audience=f"http://{GDX_HOST}/mcp", issuer=f"http://{GDX_HOST}",
    )
    assert claims["aud"] == f"http://{GDX_HOST}/mcp"
    assert claims["iss"] == f"http://{GDX_HOST}"
    assert claims["gdx_tid"] == str(GDX_UUID)
    assert claims["scope"] == "mcp:invoke"
    # MCP-bound JWT path explicitly omits refresh_token (D-S4-01).
    assert "refresh_token" not in body


# ── security: caller-supplied tenant_id must NOT override request.state ──


def test_caller_supplied_tenant_id_is_overridden_by_tenantmiddleware(client, seeded_app):
    """Pre-S4 bug: /oauth/authorize accepted tenant_id as a query param,
    so a caller at gdx.* could mint a code bound to acme by passing
    `?tenant_id=<acme-uuid>`. After S4 the middleware-resolved tenant
    overrides any caller-supplied value."""
    v, ch = _verifier_and_challenge()
    code = _authorize_under_tenant(
        client, seeded_app, GDX_HOST, ch,
        malicious_tenant_id=str(ACME_UUID),  # caller tries to forge tenant
        extra_params={"resource": f"http://{GDX_HOST}/mcp"},
    )
    r = _redeem(client, seeded_app, code, v, GDX_HOST,
                resource=f"http://{GDX_HOST}/mcp")
    assert r.status_code == 200, r.text
    claims = pyjwt.decode(
        r.json()["access_token"], _read_secret(), algorithms=["HS256"],
        audience=f"http://{GDX_HOST}/mcp", issuer=f"http://{GDX_HOST}",
    )
    # Token tenant is GDX (from middleware), NOT ACME (from query param).
    assert claims["gdx_tid"] == str(GDX_UUID)
    assert claims["gdx_tid"] != str(ACME_UUID)


# ── invalid-target: cross-resource handout is rejected ──────────────────────


def test_token_request_with_mismatched_resource_is_rejected(client, seeded_app):
    """RFC 8707 §2.3 — if the token-request resource does not match the
    code-bound resource, fail (no silent broadening)."""
    v, ch = _verifier_and_challenge()
    code = _authorize_under_tenant(
        client, seeded_app, GDX_HOST, ch,
        extra_params={"resource": f"http://{GDX_HOST}/mcp"},
    )
    r = _redeem(client, seeded_app, code, v, GDX_HOST,
                resource=f"http://acme.example.com/mcp")  # different
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["error"] == "invalid_target"


# ── legacy path: no resource indicator → opaque token (back-compat) ─────────


def test_token_without_resource_returns_opaque_token(client, seeded_app):
    """Legacy non-MCP flows that don't request a resource MUST keep
    getting opaque tokens — S4 is additive, not a breaking change."""
    v, ch = _verifier_and_challenge()
    code = _authorize_under_tenant(client, seeded_app, GDX_HOST, ch)
    r = _redeem(client, seeded_app, code, v, GDX_HOST)
    assert r.status_code == 200, r.text
    body = r.json()
    # Opaque token shape: refresh_token present, JWT structure absent.
    assert "refresh_token" in body
    assert "." not in body["access_token"]  # JWT has dots; opaque urlsafe doesn't
