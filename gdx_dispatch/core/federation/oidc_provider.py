"""SS-31 slice B — external OIDC provider adapter.

Implements the SP-side pieces of an OIDC Authorization Code + PKCE flow
against a customer-brought IdP (Authentik / Okta / Azure AD / etc.):

  * Discovery via ``.well-known/openid-configuration`` (delegated to
    ``trust_bundle.load_oidc_bundle`` so JWKS are cached with TTL).
  * State + nonce minting and validation (anti-CSRF + anti-replay).
  * Authorization URL construction.
  * ID token verification: signature (RS256) via JWKS + full claim
    validation (``iss``, ``aud``, ``exp``, ``iat``, ``nonce``). All
    required — no silent skips, no "best effort" verification.

Deliberate non-goals (integration-time work):
  * Token exchange HTTP call (the router does this using a real HTTP
    client; kept there so this module stays pure / unit-testable).
  * Refresh token handling.
  * PKCE verifier persistence — the router stashes it alongside state.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

from gdx_dispatch.core.federation.trust_bundle import (
    TrustBundle,
    TrustBundleError,
    b64url_decode,
)


class OIDCError(Exception):
    """Raised on any OIDC validation failure. Reason is machine-readable
    so the router can map it to the right federation error body."""

    def __init__(self, reason: str, *, detail: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass
class StateNonce:
    """Short-lived pair minted per login attempt. The router persists
    this (keyed by state) and replays the nonce during callback."""

    state: str
    nonce: str
    pkce_verifier: str
    created_at: float


# ---------------------------------------------------------------------------
# Mint state / nonce / PKCE
# ---------------------------------------------------------------------------


def mint_state_nonce() -> StateNonce:
    return StateNonce(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        pkce_verifier=secrets.token_urlsafe(64),
        created_at=time.time(),
    )


def pkce_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    import base64

    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Authorize URL
# ---------------------------------------------------------------------------


def build_authorization_url(
    bundle: TrustBundle,
    *,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    nonce: str,
    pkce_verifier: str,
) -> str:
    if bundle.kind != "oidc" or not bundle.authorization_endpoint:
        raise OIDCError("not_oidc_bundle")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce_challenge_s256(pkce_verifier),
        "code_challenge_method": "S256",
    }
    return f"{bundle.authorization_endpoint}?{urlencode(params)}"


def validate_state(expected: StateNonce, got_state: str) -> None:
    if not expected or not got_state:
        raise OIDCError("state_missing")
    # constant-time compare
    if not hmac.compare_digest(expected.state, got_state):
        raise OIDCError("state_mismatch")


# ---------------------------------------------------------------------------
# ID token verification
# ---------------------------------------------------------------------------


DEFAULT_CLOCK_SKEW_SECONDS = 60


def verify_id_token(
    id_token: str,
    *,
    bundle: TrustBundle,
    expected_audience: str,
    expected_nonce: Optional[str],
    now: Optional[float] = None,
    clock_skew: int = DEFAULT_CLOCK_SKEW_SECONDS,
) -> dict[str, Any]:
    """Verify signature + required claims. Returns decoded claims dict.

    Enforces ALL of: signature (RS256 via JWKS kid match), iss == bundle
    issuer, aud contains expected_audience, iat not in the future, exp
    not in the past, nonce matches expected_nonce if provided.

    Raises OIDCError with a reason code — never returns a falsy sentinel
    that callers could mistake for success.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        raise OIDCError("malformed_token")
    header_b64, payload_b64, sig_b64 = parts
    try:
        header = json.loads(b64url_decode(header_b64))
        claims = json.loads(b64url_decode(payload_b64))
        signature = b64url_decode(sig_b64)
    except Exception as exc:  # noqa: BLE001
        raise OIDCError("malformed_token", detail=str(exc)) from exc

    alg = header.get("alg")
    if alg != "RS256":
        # We deliberately reject HS256 / none. Enterprise IdPs use RS256.
        raise OIDCError("unsupported_alg", detail=str(alg))

    kid = header.get("kid")
    key = _find_jwk(bundle, kid)
    if key is None:
        raise OIDCError("unknown_kid", detail=str(kid))

    _verify_rs256(key, signed=f"{header_b64}.{payload_b64}".encode("ascii"), signature=signature)

    # --- claim validation --------------------------------------------------
    now_ts = now if now is not None else time.time()

    iss = claims.get("iss")
    if not iss or iss != bundle.issuer:
        raise OIDCError("issuer_mismatch", detail=f"{iss!r} != {bundle.issuer!r}")

    aud = claims.get("aud")
    aud_list = aud if isinstance(aud, list) else [aud]
    if expected_audience not in aud_list:
        raise OIDCError("audience_mismatch", detail=str(aud))

    exp = claims.get("exp")
    if exp is None or now_ts > float(exp) + clock_skew:
        raise OIDCError("token_expired")

    iat = claims.get("iat")
    if iat is None or float(iat) > now_ts + clock_skew:
        raise OIDCError("iat_in_future")

    if expected_nonce is not None:
        got_nonce = claims.get("nonce")
        if not got_nonce or not hmac.compare_digest(got_nonce, expected_nonce):
            raise OIDCError("nonce_mismatch")

    return claims


def _find_jwk(bundle: TrustBundle, kid: Optional[str]) -> Optional[dict[str, Any]]:
    keys = (bundle.jwks or {}).get("keys") or []
    if kid:
        for k in keys:
            if k.get("kid") == kid:
                return k
    # If the IdP only ships one key and omits kid, accept it.
    if len(keys) == 1 and not kid:
        return keys[0]
    return None


def _verify_rs256(jwk: dict[str, Any], *, signed: bytes, signature: bytes) -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        n = int.from_bytes(b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(b64url_decode(jwk["e"]), "big")
        pub = RSAPublicNumbers(e=e, n=n).public_key()
    except Exception as exc:  # noqa: BLE001
        raise OIDCError("jwk_parse_failed", detail=str(exc)) from exc
    try:
        pub.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
    except Exception as exc:  # noqa: BLE001
        raise OIDCError("signature_invalid", detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Profile extraction — feeds identity_linking
# ---------------------------------------------------------------------------


def claims_to_profile(claims: dict[str, Any]) -> dict[str, Any]:
    """Normalize OIDC claims to the shape expected by
    ``reconcile_federated_identity``.

    Keeps only fields we promise to persist. Anything else stays in the
    event payload for audit but never touches the Identity row.
    """
    return {
        "external_subject": str(claims.get("sub", "")),
        "email": claims.get("email"),
        "email_verified": bool(claims.get("email_verified", False)),
        "name": claims.get("name")
        or f"{claims.get('given_name', '')} {claims.get('family_name', '')}".strip()
        or None,
        "given_name": claims.get("given_name"),
        "family_name": claims.get("family_name"),
        "preferred_username": claims.get("preferred_username"),
    }
