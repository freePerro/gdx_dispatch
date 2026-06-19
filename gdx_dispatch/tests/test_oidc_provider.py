"""SS-31 slice B — OIDC provider tests: authorize URL, state, ID token
verification (signature + all required claims)."""
from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from gdx_dispatch.core.federation import oidc_provider as op
from gdx_dispatch.core.federation.trust_bundle import TrustBundle


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def bundle(rsa_key):
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
        provider_id="p",
        kind="oidc",
        issuer="https://idp.example.com/",
        jwks={"keys": [jwk]},
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token",
        fetched_at=time.time(),
    )


def _mint_id_token(rsa_key, *, kid="k1", claims_override=None, alg="RS256"):
    header = {"alg": alg, "kid": kid, "typ": "JWT"}
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.com/",
        "aud": "gdx-client",
        "sub": "user-42",
        "exp": now + 300,
        "iat": now,
        "nonce": "nonce-abc",
        "email": "a@b.co",
        "email_verified": True,
        "name": "Ada Lovelace",
    }
    if claims_override:
        claims.update(claims_override)
    h_b = _b64url(json.dumps(header).encode())
    p_b = _b64url(json.dumps(claims).encode())
    signing_input = f"{h_b}.{p_b}".encode()
    sig = rsa_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{h_b}.{p_b}.{_b64url(sig)}"


# ---------------------------------------------------------------------------


def test_mint_state_nonce_uniqueness():
    a = op.mint_state_nonce()
    b = op.mint_state_nonce()
    assert a.state != b.state and a.nonce != b.nonce and a.pkce_verifier != b.pkce_verifier


def test_build_authorization_url_contains_required_params(bundle):
    sn = op.mint_state_nonce()
    url = op.build_authorization_url(
        bundle,
        client_id="cid",
        redirect_uri="https://sp/callback",
        scope="openid email profile",
        state=sn.state,
        nonce=sn.nonce,
        pkce_verifier=sn.pkce_verifier,
    )
    for needle in ("response_type=code", "client_id=cid", "code_challenge_method=S256", f"state={sn.state}"):
        assert needle in url


def test_validate_state_constant_time():
    sn = op.mint_state_nonce()
    op.validate_state(sn, sn.state)
    with pytest.raises(op.OIDCError):
        op.validate_state(sn, sn.state + "x")
    with pytest.raises(op.OIDCError):
        op.validate_state(sn, "")


def test_verify_id_token_happy_path(bundle, rsa_key):
    tok = _mint_id_token(rsa_key)
    claims = op.verify_id_token(
        tok, bundle=bundle, expected_audience="gdx-client", expected_nonce="nonce-abc"
    )
    assert claims["sub"] == "user-42"


def test_verify_id_token_rejects_bad_signature(bundle, rsa_key):
    tok = _mint_id_token(rsa_key)
    tampered = tok[:-4] + "AAAA"
    with pytest.raises(op.OIDCError) as ei:
        op.verify_id_token(
            tampered, bundle=bundle, expected_audience="gdx-client", expected_nonce="nonce-abc"
        )
    assert ei.value.reason == "signature_invalid"


def test_verify_id_token_rejects_wrong_issuer(bundle, rsa_key):
    tok = _mint_id_token(rsa_key, claims_override={"iss": "https://evil/"})
    with pytest.raises(op.OIDCError) as ei:
        op.verify_id_token(tok, bundle=bundle, expected_audience="gdx-client", expected_nonce="nonce-abc")
    assert ei.value.reason == "issuer_mismatch"


def test_verify_id_token_rejects_wrong_audience(bundle, rsa_key):
    tok = _mint_id_token(rsa_key, claims_override={"aud": "someone-else"})
    with pytest.raises(op.OIDCError) as ei:
        op.verify_id_token(tok, bundle=bundle, expected_audience="gdx-client", expected_nonce="nonce-abc")
    assert ei.value.reason == "audience_mismatch"


def test_verify_id_token_rejects_expired(bundle, rsa_key):
    tok = _mint_id_token(rsa_key, claims_override={"exp": int(time.time()) - 3600})
    with pytest.raises(op.OIDCError) as ei:
        op.verify_id_token(tok, bundle=bundle, expected_audience="gdx-client", expected_nonce="nonce-abc")
    assert ei.value.reason == "token_expired"


def test_verify_id_token_rejects_bad_nonce(bundle, rsa_key):
    tok = _mint_id_token(rsa_key)
    with pytest.raises(op.OIDCError) as ei:
        op.verify_id_token(tok, bundle=bundle, expected_audience="gdx-client", expected_nonce="wrong")
    assert ei.value.reason == "nonce_mismatch"


def test_verify_id_token_rejects_none_alg(bundle, rsa_key):
    # Attacker tries alg=none
    header = {"alg": "none", "kid": "k1", "typ": "JWT"}
    claims = {"iss": "https://idp.example.com/", "aud": "gdx-client", "sub": "x",
              "exp": int(time.time()) + 300, "iat": int(time.time())}
    h_b = _b64url(json.dumps(header).encode())
    p_b = _b64url(json.dumps(claims).encode())
    tok = f"{h_b}.{p_b}."
    with pytest.raises(op.OIDCError) as ei:
        op.verify_id_token(tok, bundle=bundle, expected_audience="gdx-client", expected_nonce=None)
    assert ei.value.reason == "unsupported_alg"


def test_claims_to_profile_happy():
    p = op.claims_to_profile({"sub": "x", "email": "a@b.co", "email_verified": True, "name": "Ada"})
    assert p["external_subject"] == "x"
    assert p["email"] == "a@b.co"
