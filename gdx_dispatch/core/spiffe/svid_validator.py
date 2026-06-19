"""SS-32 slice B — SVID validator (X.509-SVID + JWT-SVID).

A SPIFFE Verifiable Identity Document (SVID) is the cryptographic proof
that a workload is who its SPIFFE ID says it is. Two serialization forms
are defined by the SPIFFE SVID spec:

* **X.509-SVID** — a standard X.509 certificate whose ``subjectAltName``
  contains exactly one URI SAN encoding the SPIFFE ID, signed by a CA in
  the trust bundle for the workload's trust domain.
* **JWT-SVID** — a JWS-signed JWT whose ``sub`` claim is the SPIFFE ID,
  ``aud`` includes the relying party's expected audience, signed by a
  key in the trust bundle's JWKS.

This module provides pure functions — no network, no filesystem. The
trust bundle is injected by the caller (see :mod:`spire_trust_bundle`).

Validation rules
----------------

X.509-SVID:

* certificate must be currently valid (``not_before`` ≤ now ≤
  ``not_after``) with up to 60s of clock-skew tolerance
* exactly one URI SAN, parseable as a valid SPIFFE ID
* SPIFFE ID's trust domain must match a CA in the supplied trust bundle
* signature of the leaf must verify against that CA's public key

JWT-SVID:

* ``alg`` must be in the allowed set (``RS256``, ``ES256``, ``EdDSA``)
* signature must verify against a JWKS key in the trust bundle
* ``sub`` must be a valid SPIFFE ID for a known trust domain
* ``aud`` (str or list) must include at least one of the expected
  audiences
* ``exp`` present and not in the past
* ``iat`` present and not in the future (≤60s skew)

Failure paths raise :class:`SVIDError` with a specific reason — the
middleware translates these into a single "reject + emit event" code
path so we never silently accept a malformed SVID.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

import logging

import jwt
from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509 import UniformResourceIdentifier
from cryptography.x509.oid import ExtensionOID

logger = logging.getLogger(__name__)

from gdx_dispatch.core.spiffe.spiffe_id import (
    SpiffeID,
    SpiffeIdError,
    parse_spiffe_id,
)

# Clock-skew tolerance for exp/iat/nbf in seconds.
CLOCK_SKEW_SECONDS = 60

ALLOWED_JWT_ALGS = ("RS256", "ES256", "EdDSA")


class SVIDError(ValueError):
    """Base class for SVID validation failures."""


class X509SVIDError(SVIDError):
    """X.509-SVID failed validation."""


class JWTSVIDError(SVIDError):
    """JWT-SVID failed validation."""


@dataclass(frozen=True)
class ValidatedSVID:
    """Result of a successful SVID validation.

    ``spiffe_id`` is the workload identity; ``kind`` is ``"x509"`` or
    ``"jwt"``; ``claims`` carries JWT-SVID claims (empty for x509);
    ``expires_at`` is the serialized not-after/exp (UTC datetime).
    """

    spiffe_id: SpiffeID
    kind: str
    expires_at: datetime
    claims: Dict[str, Any]


# ---------------------------------------------------------------------------
# Trust bundle shape
# ---------------------------------------------------------------------------
#
# The trust bundle is a mapping ``trust_domain -> {"x509_authorities":
# [PEM, ...], "jwt_authorities": [JWK dict, ...]}``. This is the shape
# produced by :mod:`spire_trust_bundle` — see that module for fetch/cache.


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _extract_spiffe_uri_san(cert: x509.Certificate) -> str:
    try:
        san_ext = cert.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        )
    except x509.ExtensionNotFound:
        raise X509SVIDError("certificate has no subjectAltName extension")
    uris = san_ext.value.get_values_for_type(UniformResourceIdentifier)
    if len(uris) == 0:
        raise X509SVIDError("certificate SAN has no URI entries")
    if len(uris) > 1:
        raise X509SVIDError("certificate SAN has multiple URI entries")
    return uris[0]


def _load_x509_authorities(
    bundle: Mapping[str, Any], trust_domain: str
) -> List[x509.Certificate]:
    td_entry = bundle.get(trust_domain)
    if not td_entry:
        raise X509SVIDError(
            f"no trust bundle entry for trust domain '{trust_domain}'"
        )
    pems = td_entry.get("x509_authorities") or []
    certs: List[x509.Certificate] = []
    for pem in pems:
        if isinstance(pem, str):
            pem_bytes = pem.encode("utf-8")
        else:
            pem_bytes = pem
        try:
            certs.append(x509.load_pem_x509_certificate(pem_bytes))
        except Exception as exc:  # pragma: no cover - defensive
            raise X509SVIDError(f"invalid CA cert in bundle: {exc}") from exc
    if not certs:
        raise X509SVIDError(
            f"trust bundle for '{trust_domain}' has no x509 authorities"
        )
    return certs


def _verify_cert_signature(
    leaf: x509.Certificate, ca: x509.Certificate
) -> bool:
    pub = ca.public_key()
    sig = leaf.signature
    tbs = leaf.tbs_certificate_bytes
    try:
        if isinstance(pub, rsa.RSAPublicKey):
            pub.verify(
                sig,
                tbs,
                padding.PKCS1v15(),
                leaf.signature_hash_algorithm,  # type: ignore[arg-type]
            )
        elif isinstance(pub, ec.EllipticCurvePublicKey):
            pub.verify(
                sig,
                tbs,
                ec.ECDSA(leaf.signature_hash_algorithm),  # type: ignore[arg-type]
            )
        else:
            # ed25519 etc — cryptography supports verify with no extra args
            pub.verify(sig, tbs)  # type: ignore[call-arg]
        return True
    except InvalidSignature:
        return False
    except (UnsupportedAlgorithm, TypeError, ValueError) as exc:
        # Key/signature shape mismatch (e.g. wrong curve, malformed sig
        # bytes, unsupported hash). Log loudly — this is NOT a routine
        # "bad signature" and should be audited as a configuration /
        # trust-bundle defect rather than treated as a silent reject.
        logger.error(
            "svid_validator.cert_signature_verify_errored",
            extra={
                "op": "verify_cert_signature",
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )
        return False


def validate_x509_svid(
    cert_pem: bytes | str,
    *,
    trust_bundle: Mapping[str, Any],
    now: Optional[datetime] = None,
) -> ValidatedSVID:
    """Validate an X.509-SVID against the supplied trust bundle.

    Raises :class:`X509SVIDError` on any validation failure.
    """
    pem_bytes = cert_pem.encode("utf-8") if isinstance(cert_pem, str) else cert_pem
    try:
        leaf = x509.load_pem_x509_certificate(pem_bytes)
    except (ValueError, TypeError) as exc:
        # cryptography raises ValueError for malformed PEM / bad DER.
        # TypeError if pem_bytes is the wrong type. Anything else
        # (OSError, MemoryError) should bubble unwrapped — those are
        # infra-level problems, not SVID validation failures.
        raise X509SVIDError(f"could not parse leaf certificate: {exc}") from exc

    effective_now = now or _now()
    skew = timedelta(seconds=CLOCK_SKEW_SECONDS)
    try:
        nb = leaf.not_valid_before_utc
        na = leaf.not_valid_after_utc
    except AttributeError:  # pragma: no cover - cryptography<42
        nb = leaf.not_valid_before.replace(tzinfo=timezone.utc)
        na = leaf.not_valid_after.replace(tzinfo=timezone.utc)
    if effective_now + skew < nb:
        raise X509SVIDError("certificate not yet valid")
    if effective_now - skew > na:
        raise X509SVIDError("certificate expired")

    san_uri = _extract_spiffe_uri_san(leaf)
    try:
        sid = parse_spiffe_id(san_uri)
    except SpiffeIdError as exc:
        raise X509SVIDError(f"SAN URI is not a valid SPIFFE ID: {exc}") from exc

    cas = _load_x509_authorities(trust_bundle, sid.trust_domain)
    if not any(_verify_cert_signature(leaf, ca) for ca in cas):
        raise X509SVIDError(
            "leaf signature did not verify against any trust-bundle CA"
        )

    return ValidatedSVID(
        spiffe_id=sid,
        kind="x509",
        expires_at=na,
        claims={},
    )


# ---------------------------------------------------------------------------
# JWT-SVID
# ---------------------------------------------------------------------------


def _load_jwt_authorities(
    bundle: Mapping[str, Any], trust_domain: str
) -> List[Dict[str, Any]]:
    td_entry = bundle.get(trust_domain)
    if not td_entry:
        raise JWTSVIDError(
            f"no trust bundle entry for trust domain '{trust_domain}'"
        )
    jwks = td_entry.get("jwt_authorities") or []
    if not jwks:
        raise JWTSVIDError(
            f"trust bundle for '{trust_domain}' has no JWT authorities"
        )
    return list(jwks)


def _jwk_to_pyjwt_key(jwk: Mapping[str, Any]):
    """Convert a JWK dict to a pyjwt-compatible key."""
    from jwt.algorithms import (
        ECAlgorithm,
        OKPAlgorithm,
        RSAAlgorithm,
    )

    kty = jwk.get("kty")
    if kty == "RSA":
        return RSAAlgorithm.from_jwk(jwk)
    if kty == "EC":
        return ECAlgorithm.from_jwk(jwk)
    if kty == "OKP":
        return OKPAlgorithm.from_jwk(jwk)
    raise JWTSVIDError(f"unsupported JWK kty '{kty}'")


def _audience_matches(
    aud_claim: Any, expected: Iterable[str]
) -> bool:
    expected_set = set(expected)
    if not expected_set:
        return False
    if isinstance(aud_claim, str):
        return aud_claim in expected_set
    if isinstance(aud_claim, list):
        return any(a in expected_set for a in aud_claim if isinstance(a, str))
    return False


def validate_jwt_svid(
    token: str,
    *,
    trust_bundle: Mapping[str, Any],
    expected_audiences: Iterable[str],
    now: Optional[datetime] = None,
) -> ValidatedSVID:
    """Validate a JWT-SVID.

    ``expected_audiences`` is a non-empty iterable of audience strings
    any one of which is acceptable. The token's trust domain is derived
    from the ``sub`` claim (the SPIFFE ID) BEFORE signature verification
    is attempted, because we need the trust domain to know which JWKS to
    use; the final accept decision still requires a valid signature.
    """
    if not isinstance(token, str) or not token:
        raise JWTSVIDError("jwt-svid token missing")
    expected_audiences = list(expected_audiences)
    if not expected_audiences:
        raise JWTSVIDError("expected_audiences must be non-empty")

    try:
        unverified_header = jwt.get_unverified_header(token)
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise JWTSVIDError(f"malformed jwt-svid: {exc}") from exc

    alg = unverified_header.get("alg")
    if alg not in ALLOWED_JWT_ALGS:
        raise JWTSVIDError(f"disallowed JWT alg '{alg}'")

    sub = unverified.get("sub")
    if sub is None:
        raise JWTSVIDError("jwt-svid missing 'sub' claim")
    try:
        sid = parse_spiffe_id(sub)
    except SpiffeIdError as exc:
        raise JWTSVIDError(f"'sub' is not a valid SPIFFE ID: {exc}") from exc

    if not _audience_matches(unverified.get("aud"), expected_audiences):
        raise JWTSVIDError("jwt-svid 'aud' does not include an expected audience")

    effective_now = now or _now()
    skew = CLOCK_SKEW_SECONDS

    iat = unverified.get("iat")
    if iat is None:
        raise JWTSVIDError("jwt-svid missing 'iat' claim")
    try:
        iat_dt = datetime.fromtimestamp(int(iat), tz=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise JWTSVIDError(f"invalid 'iat' claim: {exc}") from exc
    if iat_dt > effective_now + timedelta(seconds=skew):
        raise JWTSVIDError("jwt-svid 'iat' is in the future")

    exp = unverified.get("exp")
    if exp is None:
        raise JWTSVIDError("jwt-svid missing 'exp' claim")
    try:
        exp_dt = datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise JWTSVIDError(f"invalid 'exp' claim: {exc}") from exc
    if exp_dt + timedelta(seconds=skew) < effective_now:
        raise JWTSVIDError("jwt-svid expired")

    jwks = _load_jwt_authorities(trust_bundle, sid.trust_domain)
    kid = unverified_header.get("kid")

    last_err: Optional[Exception] = None
    for jwk in jwks:
        if kid and jwk.get("kid") and jwk.get("kid") != kid:
            continue
        try:
            key = _jwk_to_pyjwt_key(jwk)
            # Let pyjwt do signature + exp/iat verification strictly.
            jwt.decode(
                token,
                key=key,
                algorithms=[alg],
                audience=expected_audiences,
                leeway=skew,
                options={"require": ["exp", "iat", "sub", "aud"]},
            )
            return ValidatedSVID(
                spiffe_id=sid,
                kind="jwt",
                expires_at=exp_dt,
                claims=dict(unverified),
            )
        except jwt.PyJWTError as exc:
            # Expected per-key failure: this JWK didn't sign this token.
            # Remember and try the next one.
            last_err = exc
            continue
        except (JWTSVIDError, ValueError, TypeError) as exc:
            # JWK couldn't be converted to a pyjwt key (bad JWK shape).
            # Not a "try next key silently" case — log and keep trying
            # so one bad key in the bundle doesn't reject a valid token.
            logger.error(
                "svid_validator.jwk_convert_failed",
                extra={
                    "op": "jwk_to_pyjwt_key",
                    "trust_domain": sid.trust_domain,
                    "kid": jwk.get("kid"),
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )
            last_err = exc
            continue

    raise JWTSVIDError(
        f"jwt-svid signature did not verify against trust bundle: {last_err}"
    )
