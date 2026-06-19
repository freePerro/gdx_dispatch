"""SS-32 slice B tests — SVID validator (X.509 + JWT)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509 import UniformResourceIdentifier
from cryptography.x509.oid import NameOID

from gdx_dispatch.core.spiffe.svid_validator import (
    JWTSVIDError,
    X509SVIDError,
    validate_jwt_svid,
    validate_x509_svid,
)

TD = "example.com"
SID = f"spiffe://{TD}/agent/worker-1"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_rsa_keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _self_signed_ca(key, cn="test-ca"):
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert


def _leaf_cert(
    ca_key,
    ca_cert,
    spiffe_uri=SID,
    *,
    not_before_offset=timedelta(minutes=-5),
    not_after_offset=timedelta(hours=1),
    leaf_key=None,
):
    leaf_key = leaf_key or _make_rsa_keypair()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "workload")])
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + not_before_offset)
        .not_valid_after(now + not_after_offset)
    )
    if spiffe_uri is not None:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([UniformResourceIdentifier(spiffe_uri)]),
            critical=False,
        )
    cert = builder.sign(ca_key, hashes.SHA256())
    return cert, leaf_key


def _pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _bundle_with(ca_cert, td=TD, jwt_authorities=None):
    return {
        td: {
            "x509_authorities": [_pem(ca_cert)],
            "jwt_authorities": jwt_authorities or [],
        }
    }


# ---------------------------------------------------------------------------
# X.509-SVID
# ---------------------------------------------------------------------------


class TestX509SVID:
    def test_happy_path(self):
        ca_key = _make_rsa_keypair()
        ca = _self_signed_ca(ca_key)
        leaf, _ = _leaf_cert(ca_key, ca)
        result = validate_x509_svid(_pem(leaf), trust_bundle=_bundle_with(ca))
        assert str(result.spiffe_id) == SID
        assert result.kind == "x509"

    def test_expired(self):
        ca_key = _make_rsa_keypair()
        ca = _self_signed_ca(ca_key)
        leaf, _ = _leaf_cert(
            ca_key,
            ca,
            not_before_offset=timedelta(days=-2),
            not_after_offset=timedelta(days=-1),
        )
        with pytest.raises(X509SVIDError, match="expired"):
            validate_x509_svid(_pem(leaf), trust_bundle=_bundle_with(ca))

    def test_not_yet_valid(self):
        ca_key = _make_rsa_keypair()
        ca = _self_signed_ca(ca_key)
        leaf, _ = _leaf_cert(
            ca_key,
            ca,
            not_before_offset=timedelta(hours=1),
            not_after_offset=timedelta(hours=2),
        )
        with pytest.raises(X509SVIDError, match="not yet valid"):
            validate_x509_svid(_pem(leaf), trust_bundle=_bundle_with(ca))

    def test_no_san(self):
        ca_key = _make_rsa_keypair()
        ca = _self_signed_ca(ca_key)
        leaf, _ = _leaf_cert(ca_key, ca, spiffe_uri=None)
        with pytest.raises(X509SVIDError, match="subjectAltName"):
            validate_x509_svid(_pem(leaf), trust_bundle=_bundle_with(ca))

    def test_bad_spiffe_id_in_san(self):
        ca_key = _make_rsa_keypair()
        ca = _self_signed_ca(ca_key)
        leaf, _ = _leaf_cert(ca_key, ca, spiffe_uri="https://evil/x")
        with pytest.raises(X509SVIDError, match="SPIFFE ID"):
            validate_x509_svid(_pem(leaf), trust_bundle=_bundle_with(ca))

    def test_unknown_trust_domain(self):
        ca_key = _make_rsa_keypair()
        ca = _self_signed_ca(ca_key)
        leaf, _ = _leaf_cert(ca_key, ca)
        with pytest.raises(X509SVIDError, match="no trust bundle entry"):
            validate_x509_svid(_pem(leaf), trust_bundle={"other.td": {}})

    def test_signature_fails_against_wrong_ca(self):
        ca_key = _make_rsa_keypair()
        ca = _self_signed_ca(ca_key)
        other_key = _make_rsa_keypair()
        other_ca = _self_signed_ca(other_key, cn="other-ca")
        leaf, _ = _leaf_cert(ca_key, ca)
        # Bundle only has the "other" CA.
        with pytest.raises(X509SVIDError, match="did not verify"):
            validate_x509_svid(_pem(leaf), trust_bundle=_bundle_with(other_ca))


# ---------------------------------------------------------------------------
# JWT-SVID
# ---------------------------------------------------------------------------


def _rsa_jwk(key, kid="k1"):
    from jwt.algorithms import RSAAlgorithm

    pub = key.public_key()
    jwk = json.loads(RSAAlgorithm.to_jwk(pub))
    jwk["kid"] = kid
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return jwk


def _mint_jwt(key, *, sub=SID, aud="gdx-api", extra=None, kid="k1"):
    now = int(datetime.now(timezone.utc).timestamp())
    claims = {"sub": sub, "aud": aud, "iat": now, "exp": now + 300}
    if extra:
        claims.update(extra)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwt.encode(claims, priv_pem, algorithm="RS256", headers={"kid": kid})


class TestJWTSVID:
    def test_happy_path(self):
        key = _make_rsa_keypair()
        token = _mint_jwt(key)
        bundle = {TD: {"jwt_authorities": [_rsa_jwk(key)]}}
        res = validate_jwt_svid(
            token, trust_bundle=bundle, expected_audiences=["gdx-api"]
        )
        assert str(res.spiffe_id) == SID
        assert res.kind == "jwt"
        assert res.claims["sub"] == SID

    def test_expired(self):
        key = _make_rsa_keypair()
        now = int(datetime.now(timezone.utc).timestamp())
        token = _mint_jwt(key, extra={"iat": now - 3600, "exp": now - 600})
        bundle = {TD: {"jwt_authorities": [_rsa_jwk(key)]}}
        with pytest.raises(JWTSVIDError, match="expired"):
            validate_jwt_svid(
                token, trust_bundle=bundle, expected_audiences=["gdx-api"]
            )

    def test_iat_in_future(self):
        key = _make_rsa_keypair()
        now = int(datetime.now(timezone.utc).timestamp())
        token = _mint_jwt(key, extra={"iat": now + 3600, "exp": now + 7200})
        bundle = {TD: {"jwt_authorities": [_rsa_jwk(key)]}}
        with pytest.raises(JWTSVIDError, match="future"):
            validate_jwt_svid(
                token, trust_bundle=bundle, expected_audiences=["gdx-api"]
            )

    def test_aud_mismatch(self):
        key = _make_rsa_keypair()
        token = _mint_jwt(key, aud="other-service")
        bundle = {TD: {"jwt_authorities": [_rsa_jwk(key)]}}
        with pytest.raises(JWTSVIDError, match="aud"):
            validate_jwt_svid(
                token, trust_bundle=bundle, expected_audiences=["gdx-api"]
            )

    def test_bad_sub(self):
        key = _make_rsa_keypair()
        token = _mint_jwt(key, sub="not-a-spiffe-id")
        bundle = {TD: {"jwt_authorities": [_rsa_jwk(key)]}}
        with pytest.raises(JWTSVIDError, match="SPIFFE ID"):
            validate_jwt_svid(
                token, trust_bundle=bundle, expected_audiences=["gdx-api"]
            )

    def test_signature_fails(self):
        key = _make_rsa_keypair()
        other = _make_rsa_keypair()
        token = _mint_jwt(key)
        # Bundle only has the other key.
        bundle = {TD: {"jwt_authorities": [_rsa_jwk(other)]}}
        with pytest.raises(JWTSVIDError, match="did not verify"):
            validate_jwt_svid(
                token, trust_bundle=bundle, expected_audiences=["gdx-api"]
            )

    def test_unknown_trust_domain(self):
        key = _make_rsa_keypair()
        token = _mint_jwt(key)
        bundle = {"other.td": {"jwt_authorities": [_rsa_jwk(key)]}}
        with pytest.raises(JWTSVIDError, match="no trust bundle entry"):
            validate_jwt_svid(
                token, trust_bundle=bundle, expected_audiences=["gdx-api"]
            )

    def test_disallowed_alg(self):
        # Mint an HS256 token; validator should refuse before ever
        # looking at the bundle.
        import hmac
        now = int(datetime.now(timezone.utc).timestamp())
        token = jwt.encode(
            {"sub": SID, "aud": "gdx-api", "iat": now, "exp": now + 300},
            "secret",
            algorithm="HS256",
        )
        bundle = {TD: {"jwt_authorities": []}}
        with pytest.raises(JWTSVIDError, match="alg"):
            validate_jwt_svid(
                token, trust_bundle=bundle, expected_audiences=["gdx-api"]
            )

    def test_missing_expected_audiences(self):
        key = _make_rsa_keypair()
        token = _mint_jwt(key)
        bundle = {TD: {"jwt_authorities": [_rsa_jwk(key)]}}
        with pytest.raises(JWTSVIDError, match="expected_audiences"):
            validate_jwt_svid(token, trust_bundle=bundle, expected_audiences=[])
