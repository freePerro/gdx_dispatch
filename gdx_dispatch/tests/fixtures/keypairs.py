"""Deterministic RSA keypair fixture + JWT mint helper — SS-5 Slice A.

Generates one RSA-2048 keypair per pytest session (amortizes the ~50ms key
generation). The keypair is returned as PEM plus a stable ``kid`` so tests
can register the public key against an OAuthClientKey row and verify JWTs
signed with the matching private key.

``mint_installation_token`` produces a short-lived RS256 JWT with standard
claims (iss / sub / aud / exp / iat / jti / kid header) so downstream
installation-context tests have a realistic token to inject.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(scope="session")
def test_app_keypair() -> dict[str, str]:
    """Session-scoped RSA-2048 keypair.

    Returns a dict with ``private_pem``, ``public_pem``, and ``kid``.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    return {
        "private_pem": private_pem,
        "public_pem": public_pem,
        "kid": "ss5-test-kid-1",
    }


def mint_installation_token(
    *,
    keypair: dict[str, str],
    installation_id: str,
    tenant_id: str,
    oauth_client_id: str,
    capability_set_id: str | None = None,
    ttl_seconds: int = 900,
    issuer: str = "https://auth.example.com/",
    audience: str = "gdx-platform",
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Mint an installation-context RS256 JWT using the test keypair.

    Claims:
      - iss / aud / iat / exp / jti — standard JWT claims
      - sub = installation_id (subject of the token)
      - tenant_id / oauth_client_id / capability_set_id — platform-specific claims
      - Any additional keys from ``extra_claims`` override / augment

    Header:
      - alg = RS256
      - kid = keypair["kid"]

    Returns the encoded token string.
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": installation_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "jti": str(uuid4()),
        "tenant_id": tenant_id,
        "oauth_client_id": oauth_client_id,
    }
    if capability_set_id is not None:
        payload["capability_set_id"] = capability_set_id
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(
        payload,
        keypair["private_pem"],
        algorithm="RS256",
        headers={"kid": keypair["kid"]},
    )


__all__ = ["test_app_keypair", "mint_installation_token"]
