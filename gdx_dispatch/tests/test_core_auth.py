"""SS-7 Slice E — unit tests for ``gdx_dispatch.core.auth.validate_principal``.

The helper is a thin composition layer over Slice A's
``gdx_dispatch.core.auth_jwt.validate_access_token`` with Slice C's ``Denylist``
injected via Slice D's optional ``denylist=`` parameter. These tests
prove the four behaviours that matter to Slice F when it wires the
helper into the auth router:

1. A clean token validates and returns a :class:`Principal`.
2. A token whose ``jti`` is on the supplied denylist raises
   :class:`TokenRevoked` (a :class:`JWTValidationError` subclass).
3. Omitting the denylist preserves Slice A behaviour exactly — no
   revocation check fires, even when the same ``jti`` would be on a
   denylist elsewhere.
4. Malformed-token failures still surface as the typed
   :class:`JWTValidationError` subclasses callers already pattern-match.

The tests are hermetic: a per-test :class:`Denylist`, a module-scoped
RSA keypair fixture (no shared mutable state across cases), explicit
tz-aware ``expires_at`` timestamps, and the SS-6 token-shape helpers
already exercised by ``test_authentik_token_shape.py`` /
``test_auth_jwt.py``. No FastAPI app, no DB, no network — Slice E is
auth-core composition only.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gdx_dispatch.core.auth import validate_principal
from gdx_dispatch.core.auth_jwt import (
    JWTValidationError,
    MalformedToken,
    TokenRevoked,
)
from gdx_dispatch.core.denylist import Denylist
from gdx_dispatch.core.principal import ActorKind, Principal
from gdx_dispatch.tests.test_authentik_token_shape import _synthesize_spa_access_token

# ---------------------------------------------------------------------------
# Keypair + token helpers (module-scoped, deterministic)
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
def public_keys(spa_keypair) -> dict[str, bytes]:
    # Slice E only needs the SPA provider key — Slice A's allowlist still
    # covers thirdparty tokens, but the composition layer is provider-agnostic.
    return {"gdx-spa": spa_keypair[1]}


def _sign(payload: dict[str, Any], private_pem: bytes) -> str:
    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": "k1"})


def _spa_payload(**overrides: Any) -> dict[str, Any]:
    attrs = {"memberships": ["gdx"], "active_tenant": "gdx"}
    payload = _synthesize_spa_access_token(attrs, sub=str(uuid.uuid4()))
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Happy path — wrapper returns a Principal
# ---------------------------------------------------------------------------


def test_valid_token_returns_principal(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    token_jti = "ok-" + uuid.uuid4().hex
    token = _sign(_spa_payload(sub="user-spa", jti=token_jti), private_pem)

    principal = validate_principal(token, public_keys_by_provider=public_keys)

    assert isinstance(principal, Principal)
    assert principal.subject == "user-spa"
    assert principal.tenant_id == "gdx"
    assert principal.actor_kind is ActorKind.HUMAN
    assert principal.jti == token_jti


# ---------------------------------------------------------------------------
# Slice D denylist composition — revoked jti raises TokenRevoked
# ---------------------------------------------------------------------------


def test_revoked_token_raises_token_revoked(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    token_jti = "revoked-" + uuid.uuid4().hex
    payload = _spa_payload(jti=token_jti)
    token = _sign(payload, private_pem)

    denylist = Denylist()
    # Revoke past the token's natural exp so the pre-check is the rejecting layer.
    future_exp = datetime.fromtimestamp(payload["exp"] + 3600, tz=timezone.utc)
    denylist.add(token_jti, future_exp)

    with pytest.raises(TokenRevoked) as exc_info:
        validate_principal(
            token,
            public_keys_by_provider=public_keys,
            denylist=denylist,
        )
    # Middleware that catches the base class must keep working.
    assert isinstance(exc_info.value, JWTValidationError)
    assert token_jti in str(exc_info.value)


# ---------------------------------------------------------------------------
# Slice A parity — omitting denylist matches Slice A behaviour exactly
# ---------------------------------------------------------------------------


def test_denylist_omitted_preserves_slice_a_behavior(spa_keypair, public_keys):
    # Even a jti that would be on SOME denylist somewhere must validate when
    # the helper is called without a denylist argument. This pins the opt-in
    # contract Slice F will rely on during the staged router migration.
    private_pem, _ = spa_keypair
    token_jti = "would-be-revoked-" + uuid.uuid4().hex
    token = _sign(_spa_payload(jti=token_jti), private_pem)

    principal = validate_principal(token, public_keys_by_provider=public_keys)

    assert principal.jti == token_jti
    assert principal.tenant_id == "gdx"


def test_non_revoked_token_validates_with_denylist_supplied(
    spa_keypair, public_keys
):
    # Supplying a denylist that revokes a DIFFERENT jti must not block this
    # token — the wrapper composes Slice D's "is this jti on the list?"
    # check, not a "is the list non-empty?" check.
    private_pem, _ = spa_keypair
    token_jti = "clean-" + uuid.uuid4().hex
    token = _sign(_spa_payload(jti=token_jti), private_pem)

    denylist = Denylist()
    denylist.add(
        "other-jti-" + uuid.uuid4().hex,
        datetime.fromtimestamp(int(time.time()) + 3600, tz=timezone.utc),
    )

    principal = validate_principal(
        token,
        public_keys_by_provider=public_keys,
        denylist=denylist,
    )
    assert principal.jti == token_jti


# ---------------------------------------------------------------------------
# JWTValidationError passthrough — malformed tokens surface unchanged
# ---------------------------------------------------------------------------


def test_malformed_token_raises_typed_error(public_keys):
    # The wrapper must not catch-and-swallow the typed errors from Slice A;
    # router-level error mapping (Slice F) depends on the exact subclass
    # propagating up.
    with pytest.raises(MalformedToken) as exc_info:
        validate_principal("not.a.jwt", public_keys_by_provider=public_keys)
    assert isinstance(exc_info.value, JWTValidationError)


def test_malformed_token_passes_through_with_denylist(public_keys):
    # Supplying a denylist must not change the malformed-token failure
    # path — denylist handling is layered AFTER signature/claim validation
    # in Slice D, so a parse failure short-circuits before the pre-check
    # ever runs.
    denylist = Denylist()
    denylist.add(
        "any-" + uuid.uuid4().hex,
        datetime.fromtimestamp(int(time.time()) + 3600, tz=timezone.utc),
    )
    with pytest.raises(MalformedToken):
        validate_principal(
            "",
            public_keys_by_provider=public_keys,
            denylist=denylist,
        )
