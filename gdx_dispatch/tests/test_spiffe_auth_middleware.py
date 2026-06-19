"""SS-32 slice E tests — SPIFFE auth middleware."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gdx_dispatch.core.middleware.spiffe_auth_middleware import SPIFFEAuthMiddleware
from gdx_dispatch.core.spiffe.spire_trust_bundle import TrustBundleCache
from gdx_dispatch.core.spiffe.workload_capability_map import WorkloadCapabilityMap

TD = "example.com"
SID = f"spiffe://{TD}/agent/worker-1"


def _rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _rsa_jwk(key, kid="k1"):
    from jwt.algorithms import RSAAlgorithm

    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = kid
    jwk["alg"] = "RS256"
    return jwk


def _mint_jwt(key, *, sub=SID, aud="gdx-api"):
    now = int(datetime.now(timezone.utc).timestamp())
    claims = {"sub": sub, "aud": aud, "iat": now, "exp": now + 300}
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwt.encode(claims, priv_pem, algorithm="RS256", headers={"kid": "k1"})


def _bundle_cache(jwks):
    return TrustBundleCache(
        endpoint="stub",
        fetcher=lambda _ep: {TD: {"x509_authorities": [], "jwt_authorities": jwks}},
    )


def _cap_map():
    return WorkloadCapabilityMap.from_dict(
        {
            "entries": [
                {
                    "spiffe_id_glob": "spiffe://example.com/agent/**",
                    "capabilities": ["mcp:invoke"],
                    "tenant_scope": "per-tenant",
                }
            ]
        }
    )


def _app(bundle, caps, *, inject_peer_id=None):
    app = FastAPI()

    # Register SPIFFE middleware FIRST so it's inner; then inject
    # middleware becomes outer and runs before it.
    app.add_middleware(
        SPIFFEAuthMiddleware,
        trust_bundle=bundle,
        expected_audiences=["gdx-api"],
        capability_map=caps,
    )

    if inject_peer_id is not None:

        @app.middleware("http")
        async def _inject(request: Request, call_next):
            request.state.peer_spiffe_id = inject_peer_id
            return await call_next(request)

    @app.get("/whoami")
    def whoami(request: Request):
        ap = getattr(request.state, "agent_principal", None)
        if ap is None:
            return {"principal": None}
        return {
            "spiffe_id": ap.spiffe_id,
            "capabilities": list(ap.capabilities),
            "kind": ap.kind,
            "source": ap.source,
        }

    return app


def test_passthrough_when_no_spiffe_material():
    bundle = _bundle_cache([])
    app = _app(bundle, _cap_map())
    r = TestClient(app).get("/whoami")
    assert r.status_code == 200
    assert r.json() == {"principal": None}


def test_jwt_svid_header_happy_path():
    key = _rsa_key()
    bundle = _bundle_cache([_rsa_jwk(key)])
    app = _app(bundle, _cap_map())
    token = _mint_jwt(key)
    r = TestClient(app).get("/whoami", headers={"X-SPIFFE-SVID": token})
    assert r.status_code == 200
    body = r.json()
    assert body["spiffe_id"] == SID
    assert body["kind"] == "jwt"
    assert body["source"] == "header"
    assert "mcp:invoke" in body["capabilities"]


def test_bad_jwt_svid_rejected_401():
    key = _rsa_key()
    other = _rsa_key()
    bundle = _bundle_cache([_rsa_jwk(other)])  # wrong key → sig fails
    app = _app(bundle, _cap_map())
    token = _mint_jwt(key)
    r = TestClient(app).get("/whoami", headers={"X-SPIFFE-SVID": token})
    assert r.status_code == 401
    assert r.json()["error"] == "spiffe_auth_failed"


def test_mtls_peer_id_happy_path():
    bundle = _bundle_cache([])  # no JWKS needed for mTLS path
    app = _app(bundle, _cap_map(), inject_peer_id=SID)
    r = TestClient(app).get("/whoami")
    assert r.status_code == 200
    body = r.json()
    assert body["spiffe_id"] == SID
    assert body["kind"] == "x509"
    assert body["source"] == "mtls"


def test_mtls_unknown_trust_domain_rejected():
    bundle = _bundle_cache([])
    app = _app(
        bundle, _cap_map(), inject_peer_id="spiffe://other.td/agent/x"
    )
    r = TestClient(app).get("/whoami")
    assert r.status_code == 401


def test_mtls_invalid_spiffe_id_rejected():
    bundle = _bundle_cache([])
    app = _app(bundle, _cap_map(), inject_peer_id="not-a-spiffe-id")
    r = TestClient(app).get("/whoami")
    assert r.status_code == 401


def test_bundle_unavailable_rejected():
    def broken(_ep):
        raise RuntimeError("spire down")

    bundle = TrustBundleCache(endpoint="x", fetcher=broken)
    app = _app(bundle, _cap_map(), inject_peer_id=SID)
    r = TestClient(app).get("/whoami")
    assert r.status_code == 401


def test_unmatched_spiffe_id_gets_empty_caps_but_still_authenticates():
    key = _rsa_key()
    bundle = _bundle_cache([_rsa_jwk(key)])
    app = _app(bundle, _cap_map())
    token = _mint_jwt(
        key, sub="spiffe://example.com/other/role"
    )
    r = TestClient(app).get("/whoami", headers={"X-SPIFFE-SVID": token})
    assert r.status_code == 200
    assert r.json()["capabilities"] == []
