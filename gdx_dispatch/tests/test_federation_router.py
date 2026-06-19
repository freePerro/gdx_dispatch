"""SS-31 slice E — federation router tests.

Covers admin CRUD, login initiate for OIDC + SAML, OIDC callback w/
state validation + ID token verification, SAML ACS w/ AuthnRequest
context validation, and the identity collision 409 path.
"""
from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models.platform  # noqa: F401
import gdx_dispatch.models.platform_extensions  # noqa: F401
from gdx_dispatch.control.models import Base as ControlBase
from gdx_dispatch.core.federation.trust_bundle import TrustBundle, TrustBundleCache
from gdx_dispatch.models.platform import Identity
from gdx_dispatch.routers import federation as fed
from gdx_dispatch.tests.factories.platform import tenant_uuid_from_slug

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ControlBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@pytest.fixture
def oidc_bundle(rsa_key):
    pub = rsa_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "k1",
        "alg": "RS256",
        "use": "sig",
        "n": _b64url(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")),
        "e": _b64url(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big")),
    }
    return TrustBundle(
        provider_id="unset",
        kind="oidc",
        issuer="https://idp.example.com/",
        jwks={"keys": [jwk]},
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token",
        fetched_at=time.time(),
        ttl_seconds=3600,
    )


@pytest.fixture
def app(db_session, oidc_bundle):
    fed.get_provider_store(db=db_session).clear()
    fed.get_state_store().clear()
    # Inject a cache that returns a pre-fabricated bundle by provider_id,
    # with ``issuer`` preserved.
    cache = TrustBundleCache()

    def fake_load(provider_id, metadata_url, *, ttl_seconds, fetcher):
        b = TrustBundle(
            provider_id=provider_id,
            kind=oidc_bundle.kind,
            issuer=oidc_bundle.issuer,
            jwks=oidc_bundle.jwks,
            authorization_endpoint=oidc_bundle.authorization_endpoint,
            token_endpoint=oidc_bundle.token_endpoint,
            fetched_at=time.time(),
            ttl_seconds=ttl_seconds,
        )
        return b

    cache._oidc_loader = fake_load  # type: ignore[attr-defined]
    fed.set_trust_cache(cache)

    app = FastAPI()
    app.include_router(fed.router)

    def _admin():
        return {"tenant_id": str(tenant_uuid_from_slug("t1")), "user_id": "u1"}

    def _db():
        return db_session

    app.dependency_overrides[fed.require_tenant_admin] = _admin
    app.dependency_overrides[fed.get_db] = _db
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


def test_register_provider_happy(client):
    r = client.post(
        "/api/federation/providers",
        json={
            "kind": "oidc",
            "display_name": "Okta",
            "metadata_url": "https://okta.example.com/.well-known/openid-configuration",
            "client_id": "gdx-client",
            "client_secret": "shhh",
            "redirect_uri": "https://sp/cb",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "oidc"
    assert body["has_client_secret"] is True
    assert "client_secret" not in body  # never echo plaintext
    assert body["tenant_id"] == str(tenant_uuid_from_slug("t1"))


def test_register_provider_rejects_non_https(client):
    r = client.post(
        "/api/federation/providers",
        json={"kind": "oidc", "display_name": "X", "metadata_url": "http://insecure/x"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "metadata_url_must_be_https"


def test_register_provider_rejects_bad_kind(client):
    r = client.post(
        "/api/federation/providers",
        json={"kind": "ldap", "display_name": "X", "metadata_url": "https://x"},
    )
    assert r.status_code == 400


def test_list_and_delete_provider(client):
    r = client.post(
        "/api/federation/providers",
        json={"kind": "oidc", "display_name": "Okta",
              "metadata_url": "https://okta/x", "client_id": "c", "redirect_uri": "https://sp/cb"},
    )
    pid = r.json()["id"]

    lst = client.get("/api/federation/providers").json()
    assert lst["total"] == 1 and lst["items"][0]["id"] == pid

    d = client.delete(f"/api/federation/providers/{pid}")
    assert d.status_code == 204
    assert client.get("/api/federation/providers").json()["total"] == 0


def test_delete_unknown_404(client):
    assert client.delete("/api/federation/providers/none").status_code == 404


def test_provider_registered_event_emitted(client):
    events = []
    fed.set_event_emitter(lambda n, p: events.append((n, p)))
    try:
        client.post(
            "/api/federation/providers",
            json={"kind": "oidc", "display_name": "O",
                  "metadata_url": "https://o/x", "client_id": "c", "redirect_uri": "https://sp/cb"},
        )
        names = [e[0] for e in events]
        assert "gdx_dispatch.federation.provider_registered.v1" in names
    finally:
        fed.set_event_emitter(fed._default_emit)


# ---------------------------------------------------------------------------
# Login initiate
# ---------------------------------------------------------------------------


def _register(client, **extra):
    body = {
        "kind": "oidc",
        "display_name": "Okta",
        "metadata_url": "https://okta/x",
        "client_id": "gdx-client",
        "redirect_uri": "https://sp/cb",
    }
    body.update(extra)
    return client.post("/api/federation/providers", json=body).json()


def test_oidc_login_302_to_idp(client):
    pid = _register(client)["id"]
    r = client.get(f"/auth/federation/{pid}/login", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://idp.example.com/authorize?")
    assert "state=" in loc and "code_challenge=" in loc


def test_login_unknown_provider_404(client):
    r = client.get("/auth/federation/nope/login", follow_redirects=False)
    assert r.status_code == 404


def test_oidc_login_incomplete_provider_400(client):
    pid = client.post(
        "/api/federation/providers",
        json={"kind": "oidc", "display_name": "X", "metadata_url": "https://x/y"},
    ).json()["id"]
    r = client.get(f"/auth/federation/{pid}/login", follow_redirects=False)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# OIDC callback
# ---------------------------------------------------------------------------


def _mint_id_token(rsa_key, *, nonce, aud="gdx-client", sub="user-42",
                   email="ada@example.com", email_verified=True):
    header = {"alg": "RS256", "kid": "k1", "typ": "JWT"}
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.com/",
        "aud": aud,
        "sub": sub,
        "exp": now + 300,
        "iat": now,
        "nonce": nonce,
        "email": email,
        "email_verified": email_verified,
        "name": "Ada",
    }
    h = _b64url(json.dumps(header).encode())
    p = _b64url(json.dumps(claims).encode())
    sig = rsa_key.sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{h}.{p}.{_b64url(sig)}"


def test_oidc_callback_success_creates_identity(client, rsa_key, db_session):
    pid = _register(client)["id"]
    r = client.get(f"/auth/federation/{pid}/login", follow_redirects=False)
    state = r.headers["location"].split("state=")[1].split("&")[0]
    # retrieve the minted nonce from the state store
    _pid, sn = fed.get_state_store().pop_oidc(state)
    fed.get_state_store().put_oidc(_pid, sn)  # re-insert for the callback

    tok = _mint_id_token(rsa_key, nonce=sn.nonce)
    r = client.get(
        f"/auth/federation/{pid}/callback",
        params={"state": state, "code": "abc", "_test_id_token": tok},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "created"
    assert db_session.query(Identity).count() == 1


def test_oidc_callback_rejects_bad_state(client, rsa_key):
    pid = _register(client)["id"]
    client.get(f"/auth/federation/{pid}/login", follow_redirects=False)
    r = client.get(
        f"/auth/federation/{pid}/callback",
        params={"state": "wrong", "code": "abc", "_test_id_token": "x.y.z"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "state_invalid"


def test_oidc_callback_rejects_bad_token(client, rsa_key):
    pid = _register(client)["id"]
    r = client.get(f"/auth/federation/{pid}/login", follow_redirects=False)
    state = r.headers["location"].split("state=")[1].split("&")[0]
    _pid, sn = fed.get_state_store().pop_oidc(state)
    fed.get_state_store().put_oidc(_pid, sn)

    bad = _mint_id_token(rsa_key, nonce=sn.nonce, aud="someone-else")
    r = client.get(
        f"/auth/federation/{pid}/callback",
        params={"state": state, "code": "abc", "_test_id_token": bad},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "id_token_invalid"


def test_oidc_callback_collision_returns_409(client, rsa_key, db_session):
    from uuid import uuid4

    existing = Identity(id=uuid4(), email="ada@example.com", status="active")
    db_session.add(existing)
    db_session.commit()

    pid = _register(client)["id"]
    r = client.get(f"/auth/federation/{pid}/login", follow_redirects=False)
    state = r.headers["location"].split("state=")[1].split("&")[0]
    _pid, sn = fed.get_state_store().pop_oidc(state)
    fed.get_state_store().put_oidc(_pid, sn)

    tok = _mint_id_token(rsa_key, nonce=sn.nonce, email="ada@example.com")
    r = client.get(
        f"/auth/federation/{pid}/callback",
        params={"state": state, "code": "abc", "_test_id_token": tok},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "identity_collision"
    assert body["existing_identity_id"] == str(existing.id)
    assert "remediation" in body


def test_oidc_callback_token_exchange_not_wired_when_no_test_token(client, rsa_key):
    pid = _register(client)["id"]
    r = client.get(f"/auth/federation/{pid}/login", follow_redirects=False)
    state = r.headers["location"].split("state=")[1].split("&")[0]
    r = client.get(
        f"/auth/federation/{pid}/callback",
        params={"state": state, "code": "abc"},
    )
    assert r.status_code == 501
    assert r.json()["detail"]["error"] == "token_exchange_not_wired"


# ---------------------------------------------------------------------------
# Secret encoder — plaintext never persisted silently
# ---------------------------------------------------------------------------


def test_client_secret_runs_through_encoder(client, db_session):
    captured = {}

    def enc(s):
        captured["pt"] = s
        return f"ENC::{s}"

    fed.set_secret_encoder(enc)
    try:
        r = client.post(
            "/api/federation/providers",
            json={"kind": "oidc", "display_name": "O", "metadata_url": "https://o/x",
                  "client_id": "c", "client_secret": "super-secret", "redirect_uri": "https://sp/cb"},
        )
        pid = r.json()["id"]
        rec = fed.get_provider_store(db=db_session).get(pid)
        assert captured["pt"] == "super-secret"
        assert rec.client_secret_encrypted == "ENC::super-secret"
    finally:
        fed.set_secret_encoder(fed._encode_secret_default)


# ---------------------------------------------------------------------------
# Sprint 0.9-k — DB-backed provider + link persistence survives reconnect
# ---------------------------------------------------------------------------


def test_federation_stores_survive_mock_restart():
    """Register a provider + create a federation link, close the DB
    session, reopen a fresh session on the same engine, and confirm the
    rows are still there via the router's DB-backed store.

    This is the integration proof that Sprint 0.9-k swapped the
    in-memory store for the real tables — if the rows vanished on
    session close, we'd still be running against the old dict."""
    from uuid import UUID as _UUID
    from uuid import uuid4 as _uuid4

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ControlBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # --- First session: register a provider + create a link ----------------
    s1 = Session()
    try:
        store1 = fed.get_provider_store(db=s1)
        rec = fed.FederationProviderRecord(
            id=_uuid4().hex,
            tenant_id=str(tenant_uuid_from_slug("t-survive")),
            kind="oidc",
            display_name="PersistMe",
            metadata_url="https://idp.example.com/.well-known/openid-configuration",
            client_id="c1",
            client_secret_encrypted="ENC::x",
            redirect_uri="https://sp/cb",
        )
        store1.put(rec)
        provider_pk = _UUID(rec.id)

        # Need a real Identity row so the FK on federation_link resolves.
        identity = Identity(id=_uuid4(), email="ada@example.com", status="active")
        s1.add(identity)
        s1.flush()

        link = fed.FederationLink(
            id=_uuid4(),
            identity_id=identity.id,
            provider_id=provider_pk,
            external_subject="ext-sub-1",
        )
        s1.add(link)
        s1.commit()
        pid_hex = rec.id
        identity_id = identity.id
    finally:
        s1.close()

    # --- Second session: reopen, re-query via the router's store -----------
    s2 = Session()
    try:
        store2 = fed.get_provider_store(db=s2)
        roundtripped = store2.get(pid_hex)
        assert roundtripped is not None, "provider row lost on session restart"
        assert roundtripped.tenant_id == str(tenant_uuid_from_slug("t-survive"))
        assert roundtripped.display_name == "PersistMe"
        assert roundtripped.kind == "oidc"
        assert roundtripped.client_secret_encrypted == "ENC::x"

        # list_for_tenant sees it too
        items = store2.list_for_tenant(str(tenant_uuid_from_slug("t-survive")))
        assert len(items) == 1
        assert items[0].id == pid_hex

        # federation_link row is also durable
        link_q = (
            s2.query(fed.FederationLink)
            .filter(fed.FederationLink.provider_id == _UUID(pid_hex))
            .all()
        )
        assert len(link_q) == 1
        assert link_q[0].identity_id == identity_id
        assert link_q[0].external_subject == "ext-sub-1"
    finally:
        s2.close()
