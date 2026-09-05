"""SS-7 Slice A — unit tests for ``gdx_dispatch.core.auth_jwt.validate_access_token``.

Fixtures are deterministic: a session-scoped RSA keypair signs
synthetic Authentik access tokens that match the SS-6 landed shape
(``gdx-spa`` / ``gdx-thirdparty`` providers, audience ``gdx-api``,
``gdx_tid`` scope). Token bodies reuse the helpers in
``test_authentik_token_shape.py`` so the SS-6 contract surface stays
the single source of truth.

No network, no FastAPI, no DB. These tests do NOT cover middleware
wiring, JWKS fetch, denylist, or policy evaluation — those are later
SS-7 slices / follow-on SSes.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gdx_dispatch.core.auth_jwt import (
    ALLOWED_PROVIDERS,
    EXPECTED_AUDIENCE,
    ForbiddenClaim,
    InvalidAudience,
    InvalidIssuer,
    InvalidSignature,
    JWTValidationError,
    MalformedToken,
    MissingRequiredClaim,
    MissingTenantClaim,
    TokenExpired,
    TokenNotYetValid,
    TokenRevoked,
    UnsupportedProvider,
    expected_issuer,
    validate_access_token,
)
from gdx_dispatch.core.denylist import Denylist
from gdx_dispatch.core.principal import ActorKind, Principal
from gdx_dispatch.tests.test_authentik_token_shape import (
    _synthesize_spa_access_token,
    _synthesize_thirdparty_access_token,
)

# ---------------------------------------------------------------------------
# Keypair + token helpers (session-scoped, deterministic)
# ---------------------------------------------------------------------------


def _generate_keypair() -> tuple[bytes, bytes]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


@pytest.fixture(scope="module")
def spa_keypair() -> tuple[bytes, bytes]:
    return _generate_keypair()


@pytest.fixture(scope="module")
def thirdparty_keypair() -> tuple[bytes, bytes]:
    return _generate_keypair()


@pytest.fixture(scope="module")
def public_keys(spa_keypair, thirdparty_keypair) -> dict[str, bytes]:
    return {
        "gdx-spa": spa_keypair[1],
        "gdx-thirdparty": thirdparty_keypair[1],
    }


def _sign(payload: dict[str, Any], private_pem: bytes, *, kid: str = "k1") -> str:
    return jwt.encode(
        payload,
        private_pem,
        algorithm="RS256",
        headers={"kid": kid},
    )


def _spa_payload(**overrides: Any) -> dict[str, Any]:
    attrs = {"memberships": ["gdx"], "active_tenant": "gdx"}
    payload = _synthesize_spa_access_token(attrs, sub=str(uuid.uuid4()))
    payload.update(overrides)
    return payload


def _thirdparty_payload(**overrides: Any) -> dict[str, Any]:
    attrs = {"memberships": ["gdx", "acme"], "active_tenant": "acme"}
    payload = _synthesize_thirdparty_access_token(attrs, sub=str(uuid.uuid4()))
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_valid_spa_token_yields_human_principal(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    token = _sign(_spa_payload(sub="user-spa"), private_pem)

    principal = validate_access_token(token, public_keys_by_provider=public_keys)

    assert isinstance(principal, Principal)
    assert principal.provider == "gdx-spa"
    assert principal.actor_kind is ActorKind.HUMAN
    assert principal.identity_type == "human"
    assert principal.tenant_id == "gdx"
    assert principal.subject == "user-spa"
    assert principal.audience == EXPECTED_AUDIENCE
    assert principal.issuer == expected_issuer("gdx-spa")
    assert principal.expires_at > principal.issued_at


def test_valid_thirdparty_token_yields_third_party_principal(
    thirdparty_keypair, public_keys
):
    private_pem, _ = thirdparty_keypair
    token = _sign(_thirdparty_payload(sub="integration-1"), private_pem)

    principal = validate_access_token(token, public_keys_by_provider=public_keys)

    assert principal.provider == "gdx-thirdparty"
    assert principal.actor_kind is ActorKind.THIRD_PARTY
    assert principal.identity_type == "third_party"
    assert principal.tenant_id == "acme"
    assert principal.subject == "integration-1"
    assert principal.issuer == expected_issuer("gdx-thirdparty")


def test_principal_is_frozen(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    token = _sign(_spa_payload(), private_pem)
    principal = validate_access_token(token, public_keys_by_provider=public_keys)
    with pytest.raises(Exception):
        principal.tenant_id = "attacker"  # type: ignore[misc]


def test_allowed_providers_matches_ss6_allowlist():
    # Belt-and-suspenders check: this constant is what SS-7 trusts, and it
    # must stay in lock-step with SS-6's configure_authentik allowlist.
    assert set(ALLOWED_PROVIDERS) == {"gdx-spa", "gdx-thirdparty"}
    assert "gdx-mcp" not in ALLOWED_PROVIDERS


# ---------------------------------------------------------------------------
# Signature / audience / issuer failures
# ---------------------------------------------------------------------------


def test_tampered_signature_rejected(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    token = _sign(_spa_payload(), private_pem)
    # Flip one character in the signature segment.
    header_b64, payload_b64, sig_b64 = token.split(".")
    tampered_sig = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
    tampered = ".".join([header_b64, payload_b64, tampered_sig])
    with pytest.raises(InvalidSignature):
        validate_access_token(tampered, public_keys_by_provider=public_keys)


def test_wrong_provider_key_rejected(spa_keypair, thirdparty_keypair):
    # Sign a SPA-shaped token with the third-party private key, then try to
    # validate with the public-key map — the lookup by iss picks the SPA
    # public key, which will not verify the third-party signature.
    private_pem_tp, public_pem_tp = thirdparty_keypair
    _, public_pem_spa = spa_keypair
    token = _sign(_spa_payload(), private_pem_tp)
    public_keys = {"gdx-spa": public_pem_spa, "gdx-thirdparty": public_pem_tp}
    with pytest.raises(InvalidSignature):
        validate_access_token(token, public_keys_by_provider=public_keys)


def test_wrong_audience_rejected(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    token = _sign(_spa_payload(aud="not-gdx-api"), private_pem)
    with pytest.raises(InvalidAudience):
        validate_access_token(token, public_keys_by_provider=public_keys)


def test_unknown_issuer_rejected(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    token = _sign(
        _spa_payload(iss="https://auth.example.com/application/o/gdx-mcp/"),
        private_pem,
    )
    with pytest.raises(UnsupportedProvider):
        validate_access_token(token, public_keys_by_provider=public_keys)


def test_malformed_token_rejected(public_keys):
    with pytest.raises(MalformedToken):
        validate_access_token("not.a.jwt", public_keys_by_provider=public_keys)


def test_empty_token_rejected(public_keys):
    with pytest.raises(MalformedToken):
        validate_access_token("", public_keys_by_provider=public_keys)


def test_missing_iss_rejected(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    payload = _spa_payload()
    payload.pop("iss")
    token = _sign(payload, private_pem)
    with pytest.raises(MalformedToken):
        validate_access_token(token, public_keys_by_provider=public_keys)


def test_missing_public_key_for_provider_rejected(spa_keypair):
    private_pem, _ = spa_keypair
    token = _sign(_spa_payload(), private_pem)
    with pytest.raises(UnsupportedProvider):
        validate_access_token(token, public_keys_by_provider={})


# ---------------------------------------------------------------------------
# Claim-level failures
# ---------------------------------------------------------------------------


def test_missing_gdx_tid_rejected(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    payload = _spa_payload()
    payload.pop("gdx_tid")
    token = _sign(payload, private_pem)
    with pytest.raises(MissingTenantClaim):
        validate_access_token(token, public_keys_by_provider=public_keys)


def test_empty_gdx_tid_rejected(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    token = _sign(_spa_payload(gdx_tid=""), private_pem)
    with pytest.raises(MissingTenantClaim):
        validate_access_token(token, public_keys_by_provider=public_keys)


def test_non_string_gdx_tid_rejected(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    token = _sign(_spa_payload(gdx_tid=["gdx", "acme"]), private_pem)
    with pytest.raises(MissingTenantClaim):
        validate_access_token(token, public_keys_by_provider=public_keys)


def test_forbidden_tenants_array_rejected(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    # D-5 enforcement: any tokens carrying a tenants[] array are refused,
    # even if gdx_tid is also present.
    token = _sign(_spa_payload(tenants=["gdx", "acme"]), private_pem)
    with pytest.raises(ForbiddenClaim):
        validate_access_token(token, public_keys_by_provider=public_keys)


def test_missing_required_claim_rejected(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    payload = _spa_payload()
    payload.pop("sub")
    token = _sign(payload, private_pem)
    with pytest.raises(MissingRequiredClaim):
        validate_access_token(token, public_keys_by_provider=public_keys)


# ---------------------------------------------------------------------------
# Time-based failures
# ---------------------------------------------------------------------------


def test_expired_token_rejected(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    now = int(time.time()) - 3600
    payload = _spa_payload(iat=now, exp=now + 10)
    token = _sign(payload, private_pem)
    with pytest.raises(TokenExpired):
        validate_access_token(token, public_keys_by_provider=public_keys)


def test_not_yet_valid_token_rejected(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    now = int(time.time())
    payload = _spa_payload(nbf=now + 3600, iat=now, exp=now + 7200)
    token = _sign(payload, private_pem)
    with pytest.raises(TokenNotYetValid):
        validate_access_token(token, public_keys_by_provider=public_keys)


def test_leeway_allows_slight_clock_skew(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    now = int(time.time())
    payload = _spa_payload(iat=now - 100, exp=now - 5)
    token = _sign(payload, private_pem)
    # Expired by 5s; leeway of 30s should accept it.
    principal = validate_access_token(
        token, public_keys_by_provider=public_keys, leeway_seconds=30
    )
    assert principal.tenant_id == "gdx"


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


def test_all_validation_errors_are_JWTValidationError():
    # A middleware that needs to collapse "any auth failure" into a 401 must
    # be able to catch JWTValidationError once.
    subclasses = {
        MalformedToken,
        UnsupportedProvider,
        InvalidSignature,
        InvalidIssuer,
        InvalidAudience,
        TokenExpired,
        TokenNotYetValid,
        MissingTenantClaim,
        ForbiddenClaim,
        MissingRequiredClaim,
        TokenRevoked,
    }
    for cls in subclasses:
        assert issubclass(cls, JWTValidationError), cls


# ---------------------------------------------------------------------------
# SS-7 Slice D — denylist pre-check
# ---------------------------------------------------------------------------
#
# These tests prove the Slice D contract: ``validate_access_token`` accepts
# an optional :class:`gdx_dispatch.core.denylist.Denylist`, and when supplied, a
# token whose ``jti`` is present (and not expired) on the list is rejected
# with :class:`TokenRevoked` BEFORE the :class:`Principal` is built. The
# denylist parameter is opt-in — Slice A callers that don't supply one see
# no behavior change. Tests are hermetic: per-test ``Denylist()`` instances
# (no shared mutable state), explicit ``expires_at`` timestamps, and the
# existing ``_sign`` / ``_spa_payload`` / ``public_keys`` fixtures.


def test_revoked_token_rejected_with_typed_error(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    token_jti = "revoked-jti-" + uuid.uuid4().hex
    payload = _spa_payload(jti=token_jti)
    token = _sign(payload, private_pem)

    denylist = Denylist()
    # Revoke until well after the token's natural exp so the pre-check hits.
    future_exp = datetime.fromtimestamp(payload["exp"] + 3600, tz=timezone.utc)
    denylist.add(token_jti, future_exp)

    with pytest.raises(TokenRevoked) as exc_info:
        validate_access_token(
            token,
            public_keys_by_provider=public_keys,
            denylist=denylist,
        )
    # The typed error is a JWTValidationError subclass so middleware that
    # catches the base class continues to work.
    assert isinstance(exc_info.value, JWTValidationError)
    assert token_jti in str(exc_info.value)


def test_non_revoked_token_validates_with_denylist_supplied(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    token_jti = "clean-jti-" + uuid.uuid4().hex
    token = _sign(_spa_payload(sub="user-spa", jti=token_jti), private_pem)

    denylist = Denylist()
    # Revoke a DIFFERENT jti — the token under test must still validate.
    other_jti = "other-" + uuid.uuid4().hex
    denylist.add(
        other_jti,
        datetime.fromtimestamp(int(time.time()) + 3600, tz=timezone.utc),
    )

    principal = validate_access_token(
        token,
        public_keys_by_provider=public_keys,
        denylist=denylist,
    )
    assert isinstance(principal, Principal)
    assert principal.jti == token_jti
    assert principal.subject == "user-spa"


def test_missing_jti_with_denylist_does_not_crash(spa_keypair, public_keys):
    # Tokens without a jti claim must not raise — the denylist pre-check
    # treats missing/blank jti as a non-revoked miss, matching the Slice C
    # Denylist.contains(...) semantics. Legacy tokens that never carried a
    # jti (pre-Slice-A deployments) must keep flowing through.
    private_pem, _ = spa_keypair
    token = _sign(_spa_payload(), private_pem)  # no jti override -> absent

    denylist = Denylist()
    # Even with a blank-string entry on the list, a jti-less token is a miss.
    denylist.add(
        "",  # Slice C semantics: blank add is a silent no-op
        datetime.fromtimestamp(int(time.time()) + 3600, tz=timezone.utc),
    )

    principal = validate_access_token(
        token,
        public_keys_by_provider=public_keys,
        denylist=denylist,
    )
    assert principal.jti is None
    assert principal.tenant_id == "gdx"


def test_blank_jti_claim_with_denylist_does_not_crash(spa_keypair, public_keys):
    # A token that literally carries ``jti=""`` must also be treated as a
    # non-revoked miss — the Slice A normalization at the jti extraction
    # site converts blank strings to None, and the Slice D pre-check skips
    # denylist lookup when jti is None.
    private_pem, _ = spa_keypair
    token = _sign(_spa_payload(jti=""), private_pem)

    denylist = Denylist()
    principal = validate_access_token(
        token,
        public_keys_by_provider=public_keys,
        denylist=denylist,
    )
    assert principal.jti is None


def test_denylist_omitted_preserves_slice_a_behavior(spa_keypair, public_keys):
    # Slice A callers that do not pass a denylist must see no behavior
    # change. Concretely: even if a jti value would be on SOME denylist
    # somewhere, no check fires when the parameter is omitted.
    private_pem, _ = spa_keypair
    token_jti = "would-be-revoked-" + uuid.uuid4().hex
    token = _sign(_spa_payload(jti=token_jti), private_pem)

    principal = validate_access_token(token, public_keys_by_provider=public_keys)
    assert principal.jti == token_jti


def test_expired_denylist_entry_does_not_reject(spa_keypair, public_keys):
    # Slice C semantics: entries with expires_at <= now are treated as
    # misses AND opportunistically dropped. Slice D must inherit that —
    # a token whose jti was once revoked, but whose revocation window has
    # passed, must validate.
    private_pem, _ = spa_keypair
    token_jti = "expired-revocation-" + uuid.uuid4().hex
    token = _sign(_spa_payload(jti=token_jti), private_pem)

    denylist = Denylist()
    past_exp = datetime.fromtimestamp(int(time.time()) - 60, tz=timezone.utc)
    denylist.add(token_jti, past_exp)

    principal = validate_access_token(
        token,
        public_keys_by_provider=public_keys,
        denylist=denylist,
    )
    assert principal.jti == token_jti
