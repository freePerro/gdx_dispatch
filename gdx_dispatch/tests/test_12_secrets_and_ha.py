"""test_12_secrets_and_ha.py — Verify secrets rotation docs, JWKS route, and HA docs exist."""
from __future__ import annotations

import pathlib

# Repo root is two levels up from gdx_dispatch/tests/
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
GDX_ROOT = REPO_ROOT / "gdx_dispatch"


# ---------------------------------------------------------------------------
# Task 2: Secrets rotation doc
# ---------------------------------------------------------------------------

def test_secrets_rotation_doc_exists():
    doc = GDX_ROOT / "docs" / "SECRETS_ROTATION.md"
    assert doc.exists(), f"SECRETS_ROTATION.md not found at {doc}"
    lines = doc.read_text().splitlines()
    assert len(lines) > 50, (
        f"SECRETS_ROTATION.md only has {len(lines)} lines — expected > 50. "
        "Ensure DB credential rotation, JWT key rotation, and Stripe key rotation procedures are documented."
    )


# ---------------------------------------------------------------------------
# Task 3: Control plane HA doc
# ---------------------------------------------------------------------------

def test_control_plane_ha_doc_exists():
    doc = GDX_ROOT / "docs" / "CONTROL_PLANE_HA.md"
    assert doc.exists(), f"CONTROL_PLANE_HA.md not found at {doc}"
    lines = doc.read_text().splitlines()
    assert len(lines) > 20, (
        f"CONTROL_PLANE_HA.md only has {len(lines)} lines — expected > 20. "
        "Ensure second app instance, streaming replica setup, and failover procedure are documented."
    )


# ---------------------------------------------------------------------------
# Task 1: JWKS route registered in app
# ---------------------------------------------------------------------------

def test_jwks_route_registered():
    from gdx_dispatch.app import app
    from gdx_dispatch.tests.conftest import app_route_paths

    routes = app_route_paths(app)
    assert any("jwks.json" in path for path in routes), (
        f"/.well-known/jwks.json route not found in app routes. "
        f"Registered routes: {[p for p in routes if p]}"
    )


# ---------------------------------------------------------------------------
# Task 1+4: JWT uses asymmetric key (RS256)
# ---------------------------------------------------------------------------

def test_jwt_uses_asymmetric_key():
    """Verify the JWKS key store uses RS256 (asymmetric) signing, not HS256."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from gdx_dispatch.core.jwks import JWKSKeyStore

    ks = JWKSKeyStore()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    ks.add_key("test-kid", private_pem, public_pem)

    # sign_token must produce a valid JWT
    token = ks.sign_token({"sub": "test-user"}, kid="test-kid")
    assert isinstance(token, str) and token.count(".") == 2, "Expected a three-part JWT"

    # get_jwks must report alg=RS256 (asymmetric)
    jwks = ks.get_jwks()
    assert jwks["keys"], "JWKS returned no keys"
    for key_entry in jwks["keys"]:
        assert key_entry.get("alg") == "RS256", (
            f"Expected alg=RS256 (asymmetric), got {key_entry.get('alg')!r}. "
            "Upgrade path: replace HS256 shared secret with RS256 key pair via JWKSKeyStore."
        )
        assert key_entry.get("kty") == "RSA", f"Expected kty=RSA, got {key_entry.get('kty')!r}"
