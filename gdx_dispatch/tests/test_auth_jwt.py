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
from gdx_dispatch.core.contexts import (
    current_act_chain,
    current_installation_id,
    execution_context,
)
from gdx_dispatch.core.denylist import Denylist
from gdx_dispatch.core.principal import ActorKind, ExecutionContext, Principal
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
    # SS-8 Slice A — asUser/asApp seed fields default cleanly until SS-8
    # Slice B wires them from contextvars / signed installation tokens.
    assert principal.installation_id is None
    assert principal.act_chain == ()


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
    # SS-8 Slice A — same defaults on third-party tokens; no provider-specific
    # behavior change in this contract slice.
    assert principal.installation_id is None
    assert principal.act_chain == ()


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


# ---------------------------------------------------------------------------
# SS-8 Slice C — execution-context plumbed into Principal construction
# ---------------------------------------------------------------------------
#
# These tests prove that ``validate_access_token`` reads the Slice B
# contextvars ``current_installation_id`` and ``current_act_chain`` and
# threads their ``.get()`` values onto the returned ``Principal``. Both
# tests use the token-safe ContextVar lifecycle — ``token = var.set(...)``
# then ``var.reset(token)`` in a ``finally`` block — so context does not
# leak between tests. No router wiring, no middleware, no async; this is
# a pure validator-contract slice.


def test_validate_access_token_reads_contextvars_into_principal(
    spa_keypair, public_keys
):
    # SS-8 Slice F migration: the manual contextvar token lifecycle (a
    # try/finally pair around the two contextvar setters) is replaced by
    # the Slice D ``execution_context()`` helper. The helper guarantees a
    # LIFO ``reset(token)`` on exit (including exceptions), so the
    # post-block default-state assertions below still prove the
    # contextvars do not leak into any later test.
    private_pem, _ = spa_keypair
    token = _sign(_spa_payload(sub="user-spa-ctx"), private_pem)

    with execution_context(
        installation_id="install-abc123",
        act_chain=("app-root", "app-delegate"),
    ):
        principal = validate_access_token(
            token, public_keys_by_provider=public_keys
        )

        assert isinstance(principal, Principal)
        assert principal.installation_id == "install-abc123"
        assert principal.act_chain == ("app-root", "app-delegate")
        # act_chain must remain an immutable tuple, matching the
        # Principal.act_chain contract from Slice A.
        assert isinstance(principal.act_chain, tuple)

    # Post-exit: the contextvars are back to their defaults, proving the
    # helper's ``finally`` restored both tokens.
    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()


def test_validate_access_token_defaults_when_contextvars_unset(
    spa_keypair, public_keys
):
    # When the Slice B contextvars are NOT set in the caller's context,
    # the Principal must carry the Slice A defaults (``installation_id is
    # None`` and ``act_chain == ()``) — Slice C must not change behavior
    # for plain-user flows that never enter an asApp delegation.
    private_pem, _ = spa_keypair
    token = _sign(_spa_payload(sub="user-spa-default"), private_pem)

    # Sanity check: defaults are actually active entering this test.
    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()

    principal = validate_access_token(token, public_keys_by_provider=public_keys)

    assert principal.installation_id is None
    assert principal.act_chain == ()


# ---------------------------------------------------------------------------
# SS-9 Slice C — execution-context routed through canonical helper
# ---------------------------------------------------------------------------
#
# These tests prove that ``validate_access_token`` reads the SS-8 contextvar
# pair through the new ``current_execution_context()`` helper in
# ``gdx_dispatch.core.principal`` instead of poking the raw ContextVars itself. The
# helper-wiring case patches the helper at its import site inside
# ``gdx_dispatch.core.auth_jwt`` and asserts the patched values flow onto the
# returned ``Principal``; the default-policy case pins the design-doc
# decision that an unset execution context must yield
# ``ExecutionContext(None, ())`` and the Principal field defaults — i.e.
# the helper must not adopt strict-mode "raise on unset" behavior.


def test_validate_access_token_reads_helper_into_principal(spa_keypair, public_keys):
    private_pem, _ = spa_keypair
    token = _sign(_spa_payload(sub="user-spa-helper"), private_pem)

    fake_ctx = ExecutionContext(
        installation_id="install-Z",
        act_chain=("a",),
    )

    with patch(
        "gdx_dispatch.core.auth_jwt.current_execution_context", return_value=fake_ctx
    ) as patched_helper:
        principal = validate_access_token(token, public_keys_by_provider=public_keys)

    # The helper was actually called from inside validate_access_token —
    # this is what proves Slice C wires the helper, not the raw
    # contextvars (a stale wiring would never call the patched name).
    patched_helper.assert_called_once_with()
    assert isinstance(principal, Principal)
    assert principal.installation_id == "install-Z"
    assert principal.act_chain == ("a",)
    # act_chain must remain an immutable tuple — same Slice A contract
    # the SS-8 Slice F regression sentinel pins for the live-context path.
    assert isinstance(principal.act_chain, tuple)


def test_validate_access_token_default_execution_context_yields_empty_principal_fields(
    spa_keypair, public_keys
):
    # Pins the SS-9 Slice C default-context policy: when no
    # execution_context(...) scope is active, the helper must return
    # ExecutionContext(None, ()) (NOT raise), and the resulting Principal
    # must carry the matching Slice A field defaults. A future strict-mode
    # variant would belong at the policy layer, never in this helper.
    private_pem, _ = spa_keypair
    token = _sign(_spa_payload(sub="user-spa-default-policy"), private_pem)

    # Sanity: contextvars carry their stdlib defaults entering this test
    # so the helper genuinely runs against an unset context.
    assert current_installation_id.get() is None
    assert current_act_chain.get() == ()

    principal = validate_access_token(token, public_keys_by_provider=public_keys)

    assert principal.installation_id is None
    assert principal.act_chain == ()
    # Re-check that the Principal field types match the helper's snapshot
    # types so the contract stays unambiguous to downstream consumers.
    assert isinstance(principal.act_chain, tuple)


# ---------------------------------------------------------------------------
# SS-9 Slice E — contextvar-decoupling regression guard
# ---------------------------------------------------------------------------
#
# Slice C routed ``validate_access_token``'s execution-context read through
# the ``current_execution_context()`` helper so ``gdx_dispatch/core/auth_jwt.py`` no
# longer imports the raw contextvars from ``gdx_dispatch.core.contexts`` or calls
# ``.get()`` on them directly. This source-text guard pins that decoupling
# in place: if a future refactor re-introduces a direct contextvar import
# or read in ``auth_jwt.py``, this test fails before the change lands.
# The check is intentionally file-local (reads only ``auth_jwt.py``) — a
# whole-repo scan belongs to the migration-sweep planning slice, not here.


def test_auth_jwt_source_has_no_direct_contextvar_reads_or_imports():
    from pathlib import Path

    import gdx_dispatch.core.auth_jwt as _auth_jwt_module

    source = Path(_auth_jwt_module.__file__).read_text()

    forbidden = (
        "from gdx_dispatch.core.contexts import",
        "current_installation_id.get(",
        "current_act_chain.get(",
    )
    for needle in forbidden:
        assert needle not in source, (
            f"gdx_dispatch/core/auth_jwt.py must not contain {needle!r} — "
            "SS-9 Slice C routed execution-context reads through "
            "current_execution_context(); direct contextvar coupling is a "
            "regression."
        )


# ---------------------------------------------------------------------------
# SS-9 Slice G — repo-level contextvar import contract guard (D-ss9-1)
# ---------------------------------------------------------------------------
#
# The Slice E sentinel above is file-local to ``gdx_dispatch/core/auth_jwt.py``.
# Slice F's migration-sweep inventory showed that the canonical raw
# reader is ``gdx_dispatch/core/principal.py`` and that no other production module
# still reads the contextvars directly. This test pins that outcome at
# the repo level: if any production file under ``gdx/`` (other than the
# canonical ``gdx_dispatch/core/principal.py`` helper) re-introduces a raw import
# of ``current_installation_id`` / ``current_act_chain`` from
# ``gdx_dispatch.core.contexts``, or calls ``.get()`` on either, this test fails
# before the change lands.
#
# The scan is intentionally text-based (``re`` from stdlib only) so it
# stays deterministic and does not depend on external linters. Tests
# themselves are excluded — test modules legitimately exercise the raw
# contextvars to assert the helper contract — and the public
# ``execution_context`` / ``async_execution_context`` context-manager
# API (already imported by ``gdx_dispatch/routers/auth.py``) is explicitly
# allowed because neither name is a raw contextvar.


def test_no_production_module_outside_principal_couples_to_raw_contextvars():
    import re
    from pathlib import Path

    import gdx_dispatch as _gdx_pkg

    gdx_root = Path(_gdx_pkg.__file__).resolve().parent
    repo_root = gdx_root.parent
    tests_dir = (gdx_root / "tests").resolve()
    allowed_raw_consumer = (gdx_root / "core" / "principal.py").resolve()

    contexts_import_re = re.compile(
        r"from\s+gdx\.core\.contexts\s+import\s*(\([^)]*\)|[^\n]*)"
    )
    forbidden_name_re = re.compile(
        r"\b(current_installation_id|current_act_chain)\b"
    )
    forbidden_get_re = re.compile(
        r"\b(current_installation_id|current_act_chain)\.get\s*\("
    )

    violations: list[str] = []

    for path in sorted(gdx_root.rglob("*.py")):
        resolved = path.resolve()
        if resolved == allowed_raw_consumer:
            continue
        try:
            resolved.relative_to(tests_dir)
        except ValueError:
            pass
        else:
            continue  # exclude gdx_dispatch/tests/**

        rel_display = resolved.relative_to(repo_root).as_posix()
        source = resolved.read_text(encoding="utf-8")

        for match in contexts_import_re.finditer(source):
            payload = match.group(1)
            lineno = source.count("\n", 0, match.start()) + 1
            for bad_name in forbidden_name_re.findall(payload):
                first_line = match.group(0).splitlines()[0].strip()
                violations.append(
                    f"{rel_display}:{lineno}: raw contextvar "
                    f"{bad_name!r} imported from gdx_dispatch.core.contexts "
                    f"({first_line!r})"
                )

        for match in forbidden_get_re.finditer(source):
            lineno = source.count("\n", 0, match.start()) + 1
            offending_line = source.splitlines()[lineno - 1].strip()
            violations.append(
                f"{rel_display}:{lineno}: raw contextvar read "
                f"{match.group(1)!r}.get(...) — route through "
                f"current_execution_context() instead ({offending_line!r})"
            )

    assert not violations, (
        "SS-9 D-ss9-1 contract violation: production modules outside "
        "gdx_dispatch/core/principal.py must not couple to the raw contextvars "
        "`current_installation_id` / `current_act_chain`. Route reads "
        "through `gdx_dispatch.core.principal.current_execution_context()` "
        "instead. Violations:\n  " + "\n  ".join(violations)
    )
