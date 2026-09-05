"""Deterministic RSA keypair fixture.

Generates one RSA-2048 keypair per pytest session (amortizes the ~50ms key
generation). The keypair is returned as PEM plus a stable ``kid`` so tests
can register the public key and verify JWTs signed with the matching
private key.
"""
from __future__ import annotations

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


__all__ = ["test_app_keypair"]
