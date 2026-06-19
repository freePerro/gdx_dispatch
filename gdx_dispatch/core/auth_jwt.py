"""SS-7 Slice A+D — Authentik access token validation.

Bounded scope
-------------
This module validates Authentik-issued access tokens for the two SS-6
landed OAuth providers (``gdx-spa``, ``gdx-thirdparty``) and returns a
typed :class:`gdx_dispatch.core.principal.Principal`. It does NOT:

* fetch JWKS over HTTP (live JWKS resolver lands in a later SS-7 slice),
* wire FastAPI middleware / ``get_current_user`` (SS-9),
* evaluate capabilities or a policy engine (SS-7 Slice B).

Callers supply the trusted public key (or key resolver) out-of-band; the
live JWKS client layer (Slice B or later) composes on top of this module.

Slice D adds an optional denylist pre-check: if the caller injects an
SS-7 Slice C :class:`gdx_dispatch.core.denylist.Denylist` instance and the
token's ``jti`` is present (and not yet expired) on that list,
validation fails with :class:`TokenRevoked` before the
:class:`Principal` is constructed. Slice D keeps the denylist a pure
function-level parameter — no module-level singleton, no
``app.state`` wiring — so the authentication surface stays
deterministic at the unit-test layer. Runtime lifecycle (how the
caller obtains the denylist) is an SS-7 Slice E concern.

Security posture
----------------
* Signature verification is mandatory — ``PyJWT`` is invoked with explicit
  ``algorithms=["RS256"]`` only (OAuth 2.1). No ``verify=False`` path is
  exposed.
* Issuer is locked to the SS-6 Authentik hostname, per-provider — the
  validator refuses tokens from any other issuer.
* Audience is locked to ``gdx-api`` — matches both SS-6 provider payloads.
* ``exp`` and ``nbf`` are enforced via ``options={"require": [...]}``;
  ``exp`` missing is itself a failure.
* The ``gdx_tid`` claim (D-5 singular tenant) is mandatory; absence or
  non-string / empty value raises :class:`MissingTenantClaim`.
* ``tenants`` / ``tenants_array`` / ``tid_list`` claims are forbidden
  (D-5 enforcement) — tokens carrying any of them are rejected.
* Every exception path maps to a typed :class:`JWTValidationError`
  subclass — no ``except Exception: pass`` swallowing per CLAUDE.md Build
  Rules.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import jwt
from jwt.exceptions import (
    ExpiredSignatureError,
    ImmatureSignatureError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidSignatureError,
    InvalidTokenError,
    MissingRequiredClaimError,
)

from gdx_dispatch.core.denylist import Denylist
from gdx_dispatch.core.principal import ActorKind, Principal, current_execution_context

logger = logging.getLogger(__name__)

EXPECTED_AUDIENCE = "gdx-api"
"""Audience claim every SS-6 provider mints — matches ``GDX_SPA_AUDIENCE``
and ``GDX_THIRDPARTY_AUDIENCE`` in ``gdx_dispatch/tools/configure_authentik.py``."""

AUTHENTIK_ISSUER_BASE = "https://auth.example.com"
"""Authentik hostname. SS-6 provider issuer URLs derive from this."""

ALLOWED_PROVIDERS: tuple[str, ...] = ("gdx-spa", "gdx-thirdparty")
"""Providers whose access tokens SS-7 accepts.

Matches ``SUPPORTED_PROVIDERS`` in ``gdx_dispatch/tools/configure_authentik.py``.
``gdx-mcp`` is intentionally absent — MCP authenticates via PAT bearer
tokens (SS-14 issuance / SS-19 validation), not via JWT access tokens
minted by Authentik."""

_PROVIDER_ACTOR_KIND: dict[str, ActorKind] = {
    "gdx-spa": ActorKind.HUMAN,
    "gdx-thirdparty": ActorKind.THIRD_PARTY,
}

# D-5: claim names that MUST NOT appear on a valid token. The Authentik
# property mapping emits only ``gdx_tid`` (see
# ``authentik_property_mapping_gdx_tid.SANDBOX_EXPRESSION``); anything
# else is either attacker-crafted or a mis-configured mapping.
FORBIDDEN_CLAIMS: frozenset[str] = frozenset({"tenants", "tenants_array", "tid_list"})

# The Authentik property mapping does not yet emit ``identity_type``
# (D18 Slice A assumption). SPA tokens are treated as human until that
# column lands.
ASSUMED_HUMAN_IDENTITY_TYPE = "human"
ASSUMED_THIRD_PARTY_IDENTITY_TYPE = "third_party"


class JWTValidationError(Exception):
    """Base class for every SS-7 Slice A validation failure.

    Subclasses are stable public API — routers / middleware (SS-9) MUST
    be able to pattern-match on these exact types to decide between
    401 (token invalid) and 5xx (infrastructure).
    """


class MalformedToken(JWTValidationError):
    """Token is not a parseable JWT (bad header, wrong segment count, etc.)."""


class UnsupportedProvider(JWTValidationError):
    """Token ``iss`` does not map to a provider in :data:`ALLOWED_PROVIDERS`."""


class InvalidSignature(JWTValidationError):
    """Signature verification failed against the supplied public key."""


class InvalidIssuer(JWTValidationError):
    """``iss`` claim does not match the expected provider issuer URL."""


class InvalidAudience(JWTValidationError):
    """``aud`` claim does not match :data:`EXPECTED_AUDIENCE`."""


class TokenExpired(JWTValidationError):
    """``exp`` is in the past."""


class TokenNotYetValid(JWTValidationError):
    """``nbf`` is in the future."""


class MissingTenantClaim(JWTValidationError):
    """``gdx_tid`` claim is absent, empty, or not a string."""


class ForbiddenClaim(JWTValidationError):
    """Token carries a D-5-forbidden claim (``tenants`` / array-shape)."""


class MissingRequiredClaim(JWTValidationError):
    """A required registered claim (``iss``/``aud``/``sub``/``exp``/``iat``) is absent."""


class TokenRevoked(JWTValidationError):
    """Token's ``jti`` is on the caller-supplied SS-7 Slice C denylist.

    Raised by :func:`validate_access_token` only when the caller injects
    a :class:`gdx_dispatch.core.denylist.Denylist` via the ``denylist`` keyword
    argument AND the token presents a non-empty ``jti`` claim that the
    denylist reports as still-revoked. Middleware (SS-9) should collapse
    this to a 401 alongside the other :class:`JWTValidationError`
    subclasses; the separate type exists so audit logs can distinguish
    "token was revoked" from "token was never valid".
    """


@dataclass(frozen=True)
class _ProviderExpectations:
    provider: str
    issuer: str
    actor_kind: ActorKind
    identity_type: str


def expected_issuer(provider: str) -> str:
    """Return the Authentik issuer URL for ``provider``.

    Matches the live issuer that Authentik mints into the ``iss`` claim
    of access tokens for the SS-6 providers. Raises
    :class:`UnsupportedProvider` if ``provider`` is not in the allowlist.
    """
    if provider not in ALLOWED_PROVIDERS:
        raise UnsupportedProvider(
            f"provider {provider!r} is not in the SS-6 allowlist {ALLOWED_PROVIDERS!r}"
        )
    return f"{AUTHENTIK_ISSUER_BASE}/application/o/{provider}/"


def _provider_from_issuer(issuer: Any) -> _ProviderExpectations:
    """Derive the provider allowlist entry from an unverified ``iss`` claim.

    The ``iss`` value is only used to pick the key / provider — it is
    verified cryptographically by ``jwt.decode`` via the
    ``issuer=...`` argument before any provider metadata is trusted.
    """
    if not isinstance(issuer, str) or not issuer:
        raise MalformedToken("token is missing or has a non-string 'iss' claim")

    for provider in ALLOWED_PROVIDERS:
        if issuer == expected_issuer(provider):
            return _ProviderExpectations(
                provider=provider,
                issuer=expected_issuer(provider),
                actor_kind=_PROVIDER_ACTOR_KIND[provider],
                identity_type=(
                    ASSUMED_HUMAN_IDENTITY_TYPE
                    if _PROVIDER_ACTOR_KIND[provider] is ActorKind.HUMAN
                    else ASSUMED_THIRD_PARTY_IDENTITY_TYPE
                ),
            )

    raise UnsupportedProvider(
        f"issuer {issuer!r} does not match any SS-6 provider; "
        f"allowed providers: {ALLOWED_PROVIDERS!r}"
    )


def validate_access_token(
    token: str,
    *,
    public_keys_by_provider: Mapping[str, bytes | str],
    leeway_seconds: int = 0,
    denylist: Denylist | None = None,
) -> Principal:
    """Validate an Authentik access token and return a :class:`Principal`.

    Parameters
    ----------
    token:
        The encoded JWT (compact serialization).
    public_keys_by_provider:
        PEM-encoded RSA public keys keyed by provider slug
        (``"gdx-spa"`` / ``"gdx-thirdparty"``). Callers supply the
        trusted keys out-of-band; the JWKS-fetch client lands in a later
        SS-7 slice.
    leeway_seconds:
        Clock skew allowance in seconds applied to ``exp``/``nbf``
        checks. Defaults to zero so tests are deterministic.
    denylist:
        Optional SS-7 Slice C :class:`gdx_dispatch.core.denylist.Denylist`. When
        supplied and the token carries a non-empty ``jti`` claim, the
        ``jti`` is checked against the denylist AFTER signature and
        claim validation succeed and BEFORE the :class:`Principal` is
        built; a hit raises :class:`TokenRevoked`. When not supplied
        (the default), no revocation check is performed — this matches
        the Slice A behavior and keeps the denylist opt-in at the
        function-call boundary. Slice D deliberately does not wire a
        module-level singleton or ``app.state`` default; that lifecycle
        call is Slice E.

    Returns
    -------
    Principal
        A frozen dataclass built only from verified claims.

    Raises
    ------
    MalformedToken
        Token cannot be parsed, header is malformed, or ``iss`` is
        missing from the unverified header/payload.
    UnsupportedProvider
        Token's ``iss`` does not match any SS-6 provider.
    InvalidSignature
        Signature does not verify against the supplied public key.
    InvalidIssuer / InvalidAudience
        Strict claim checks failed despite the initial provider lookup
        (defense-in-depth against a caller passing a mismatched key).
    TokenExpired / TokenNotYetValid
        ``exp``/``nbf`` outside the allowed window.
    MissingRequiredClaim
        A registered claim required by :data:`_REQUIRED_CLAIMS` is
        absent.
    MissingTenantClaim
        ``gdx_tid`` is absent, empty, or not a string.
    ForbiddenClaim
        Token carries a D-5-forbidden claim (``tenants`` / array shape).
    TokenRevoked
        ``denylist`` was supplied and the token's ``jti`` is present
        and not yet expired on that list.
    """
    if not isinstance(token, str) or not token:
        raise MalformedToken("token must be a non-empty string")

    # Peek at the unverified payload ONLY to pick the provider key; the
    # payload is still re-validated cryptographically below via
    # ``jwt.decode`` with ``issuer=``/``audience=`` enforcement.
    try:
        unverified = jwt.decode(
            token,
            options={"verify_signature": False, "verify_exp": False, "verify_nbf": False},
        )
    except InvalidTokenError as exc:
        raise MalformedToken(f"could not parse JWT payload: {exc}") from exc

    expectations = _provider_from_issuer(unverified.get("iss"))

    public_key = public_keys_by_provider.get(expectations.provider)
    if public_key is None:
        raise UnsupportedProvider(
            f"no public key supplied for provider {expectations.provider!r}"
        )

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=EXPECTED_AUDIENCE,
            issuer=expectations.issuer,
            leeway=leeway_seconds,
            options={"require": list(_REQUIRED_CLAIMS)},
        )
    except ExpiredSignatureError as exc:
        raise TokenExpired(str(exc)) from exc
    except ImmatureSignatureError as exc:
        raise TokenNotYetValid(str(exc)) from exc
    except InvalidAudienceError as exc:
        raise InvalidAudience(str(exc)) from exc
    except InvalidIssuerError as exc:
        raise InvalidIssuer(str(exc)) from exc
    except InvalidSignatureError as exc:
        raise InvalidSignature(str(exc)) from exc
    except MissingRequiredClaimError as exc:
        raise MissingRequiredClaim(str(exc)) from exc
    except InvalidTokenError as exc:
        # Catch-all for jwt errors not mapped above (e.g. unsupported
        # algorithm). Surface as MalformedToken to keep the error taxonomy
        # closed; the underlying exception is chained for observability.
        raise MalformedToken(f"token failed validation: {exc}") from exc

    forbidden_present = FORBIDDEN_CLAIMS & claims.keys()
    if forbidden_present:
        raise ForbiddenClaim(
            f"token carries D-5-forbidden claims: {sorted(forbidden_present)!r}"
        )

    tenant_id = claims.get("gdx_tid")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise MissingTenantClaim(
            "token is missing the 'gdx_tid' claim or it is not a non-empty string"
        )

    jti_value = claims.get("jti")
    jti: str | None = jti_value if isinstance(jti_value, str) and jti_value else None

    # Slice D pre-check: reject revoked tokens BEFORE building the
    # Principal. Denylist injection is optional (default: no-op) so
    # Slice A's signature remains the minimum contract; a missing/blank
    # ``jti`` is treated as a non-revoked miss per Slice C semantics so
    # legacy tokens do not crash the revocation path.
    if denylist is not None and jti is not None and denylist.contains(jti):
        raise TokenRevoked(
            f"token jti {jti!r} is on the caller-supplied revocation denylist"
        )

    # SS-9 Slice C: route the SS-8 execution-context read through the
    # canonical helper so audit middleware and policy-input builders
    # share one snapshot site instead of re-importing the raw
    # contextvars. The helper delegates to the same two ``.get()`` calls
    # in the same order, so Slice A/B/F behavior is byte-identical when
    # the context is unset (defaults: ``None`` / ``()``) or set (matches
    # the values fed in by ``execution_context(...)``).
    exec_ctx = current_execution_context()
    return Principal(
        tenant_id=tenant_id,
        subject=str(claims["sub"]),
        provider=expectations.provider,
        actor_kind=expectations.actor_kind,
        identity_type=expectations.identity_type,
        issued_at=int(claims["iat"]),
        expires_at=int(claims["exp"]),
        issuer=expectations.issuer,
        audience=EXPECTED_AUDIENCE,
        jti=jti,
        raw_claims=dict(claims),
        installation_id=exec_ctx.installation_id,
        act_chain=exec_ctx.act_chain,
    )


_REQUIRED_CLAIMS: tuple[str, ...] = ("iss", "aud", "sub", "exp", "iat")
