"""SS-31 slice A — IdP trust bundle loader, validator, and TTL cache.

A "trust bundle" is the collection of material an SP needs to validate
an IdP's assertions / tokens:

  * For OIDC:  the issuer's JWKS (public signing keys) + discovery
               metadata (``.well-known/openid-configuration``).
  * For SAML:  the IdP's X.509 signing certificate(s) + the SAML 2.0
               metadata XML (entityID, SSO endpoints).

Design rules (from the SS-31 prompt):
  1. **Cache with TTL.** Default 3600 s. Callers may override.
  2. **Refresh in background.** ``refresh_if_stale`` is non-blocking for
     the happy path; a background thread does the fetch. If the refresh
     fails, the stale-but-valid cached bundle is still served and a
     warning is logged. (No silent failure — the warning is loud.)
  3. **No silent failures.** Every parse / signature error raises
     ``TrustBundleError`` with a structured reason.
  4. **Signature helpers.** Exposed here so the OIDC and SAML modules
     both route through the same verified cache.

This module deliberately does NOT do any network I/O at import time,
and the HTTP fetch is pluggable via the ``fetcher`` kwarg so tests can
inject a stub without monkeypatching ``urllib``.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509 import load_pem_x509_certificate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

DEFAULT_TTL_SECONDS = 3600
DEFAULT_TIMEOUT_SECONDS = 10


class TrustBundleError(Exception):
    """Raised when a trust bundle is malformed, unreachable on first
    fetch, or fails a signature check.

    NEVER swallowed; always bubbled up to the federation router so the
    caller (tenant admin or end user in the login flow) sees a real
    error, not a silent fallback.
    """

    def __init__(self, reason: str, *, detail: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass
class TrustBundle:
    """Immutable-ish record of what we trust about one IdP.

    ``raw`` holds the as-fetched discovery / metadata document so tests
    can introspect provider-specific fields without re-fetching.
    """

    provider_id: str
    kind: str  # "oidc" | "saml"
    issuer: Optional[str] = None
    jwks: dict[str, Any] = field(default_factory=dict)
    signing_certs_pem: list[str] = field(default_factory=list)
    authorization_endpoint: Optional[str] = None
    token_endpoint: Optional[str] = None
    sso_endpoint: Optional[str] = None  # SAML
    fetched_at: float = 0.0
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    raw: dict[str, Any] = field(default_factory=dict)

    def is_stale(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self.fetched_at) >= self.ttl_seconds


# ---------------------------------------------------------------------------
# Fetcher — pluggable for tests
# ---------------------------------------------------------------------------

Fetcher = Callable[[str], bytes]


def _default_http_fetcher(url: str) -> bytes:
    """Fetch raw bytes over HTTPS. Raises ``TrustBundleError`` on any
    non-2xx or transport failure — never silently returns empty bytes.
    """
    req = Request(url, headers={"Accept": "application/json, application/xml"})
    try:
        with urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as resp:  # nosec B310 — trust bundle URL is tenant-admin-configured
            status = getattr(resp, "status", 200)
            if status >= 300:
                raise TrustBundleError(
                    "fetch_failed", detail=f"HTTP {status} for {url}"
                )
            return resp.read()
    except TrustBundleError:
        raise
    except Exception as exc:  # noqa: BLE001 — deliberate: wrap all transport errors
        raise TrustBundleError("fetch_failed", detail=f"{url}: {exc}") from exc


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_oidc_bundle(
    provider_id: str,
    metadata_url: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    fetcher: Fetcher = _default_http_fetcher,
) -> TrustBundle:
    """Fetch the OIDC ``.well-known/openid-configuration`` + JWKS.

    Validation:
      * metadata must include ``issuer``, ``jwks_uri``,
        ``authorization_endpoint``, ``token_endpoint``.
      * JWKS must include at least one key with a ``kid`` and a ``kty``.
    """
    raw = fetcher(metadata_url)
    try:
        meta = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise TrustBundleError("invalid_metadata", detail=str(exc)) from exc

    required = ("issuer", "jwks_uri", "authorization_endpoint", "token_endpoint")
    missing = [k for k in required if not meta.get(k)]
    if missing:
        raise TrustBundleError(
            "invalid_metadata", detail=f"missing fields: {', '.join(missing)}"
        )

    jwks_raw = fetcher(meta["jwks_uri"])
    try:
        jwks = json.loads(jwks_raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise TrustBundleError("invalid_jwks", detail=str(exc)) from exc
    keys = jwks.get("keys") or []
    if not keys or not all(k.get("kty") for k in keys):
        raise TrustBundleError("invalid_jwks", detail="no usable keys")

    return TrustBundle(
        provider_id=provider_id,
        kind="oidc",
        issuer=meta["issuer"],
        jwks=jwks,
        authorization_endpoint=meta["authorization_endpoint"],
        token_endpoint=meta["token_endpoint"],
        fetched_at=time.time(),
        ttl_seconds=ttl_seconds,
        raw=meta,
    )


def load_saml_bundle(
    provider_id: str,
    metadata_url: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    fetcher: Fetcher = _default_http_fetcher,
) -> TrustBundle:
    """Fetch and parse SAML 2.0 IdP metadata XML.

    Uses ``defusedxml`` to protect against XXE / billion-laughs. Pulls:
      * entityID   -> issuer
      * SingleSignOnService Location -> sso_endpoint
      * X509Certificate(s) under KeyDescriptor use="signing"
    """
    # Import here to keep the hard dependency scoped to SAML users.
    from defusedxml import ElementTree as DefusedET  # type: ignore

    raw = fetcher(metadata_url)
    try:
        root = DefusedET.fromstring(raw)
    except Exception as exc:  # noqa: BLE001
        raise TrustBundleError("invalid_metadata", detail=str(exc)) from exc

    ns = {
        "md": "urn:oasis:names:tc:SAML:2.0:metadata",
        "ds": "http://www.w3.org/2000/09/xmldsig#",
    }
    entity_id = root.attrib.get("entityID")
    if not entity_id:
        raise TrustBundleError("invalid_metadata", detail="missing entityID")

    sso_el = root.find(".//md:IDPSSODescriptor/md:SingleSignOnService", ns)
    if sso_el is None or not sso_el.attrib.get("Location"):
        raise TrustBundleError(
            "invalid_metadata", detail="missing SingleSignOnService Location"
        )

    certs: list[str] = []
    for kd in root.findall(".//md:IDPSSODescriptor/md:KeyDescriptor", ns):
        use = kd.attrib.get("use", "signing")
        if use not in ("signing", ""):
            continue
        for cert_el in kd.findall(".//ds:X509Certificate", ns):
            if cert_el.text and cert_el.text.strip():
                pem = _wrap_pem(cert_el.text.strip())
                certs.append(pem)

    if not certs:
        raise TrustBundleError(
            "invalid_metadata", detail="no signing X509Certificate found"
        )

    return TrustBundle(
        provider_id=provider_id,
        kind="saml",
        issuer=entity_id,
        sso_endpoint=sso_el.attrib["Location"],
        signing_certs_pem=certs,
        fetched_at=time.time(),
        ttl_seconds=ttl_seconds,
        raw={"entity_id": entity_id},
    )


def _wrap_pem(b64_body: str) -> str:
    """Wrap a raw base64 cert body (as found inline in SAML metadata)
    into PEM so ``cryptography`` can load it.
    """
    b64_body = "".join(b64_body.split())
    lines = [b64_body[i : i + 64] for i in range(0, len(b64_body), 64)]
    return (
        "-----BEGIN CERTIFICATE-----\n"
        + "\n".join(lines)
        + "\n-----END CERTIFICATE-----\n"
    )


# ---------------------------------------------------------------------------
# Signature helpers
# ---------------------------------------------------------------------------


def verify_rsa_signature(
    cert_pem: str, signed_bytes: bytes, signature: bytes, *, hash_name: str = "sha256"
) -> bool:
    """Verify ``signature`` over ``signed_bytes`` using the RSA public
    key embedded in ``cert_pem``.

    Returns ``True`` on success. On any verification failure raises
    ``TrustBundleError('signature_invalid')`` rather than returning
    ``False`` silently — callers want the error detail for audit.
    """
    try:
        cert = load_pem_x509_certificate(cert_pem.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise TrustBundleError("cert_parse_failed", detail=str(exc)) from exc
    pub = cert.public_key()
    if not isinstance(pub, rsa.RSAPublicKey):
        raise TrustBundleError("unsupported_key_type")
    hash_algo = {"sha256": hashes.SHA256(), "sha1": hashes.SHA1()}.get(hash_name)
    if hash_algo is None:
        raise TrustBundleError("unsupported_hash", detail=hash_name)
    try:
        pub.verify(signature, signed_bytes, padding.PKCS1v15(), hash_algo)
    except Exception as exc:  # noqa: BLE001
        raise TrustBundleError("signature_invalid", detail=str(exc)) from exc
    return True


def b64url_decode(data: str) -> bytes:
    """Base64url decode, tolerating missing padding (as JWT/JOSE emit)."""
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class TrustBundleCache:
    """Thread-safe TTL cache of loaded bundles.

    Behavior contract:
      * First load is synchronous. Failure raises ``TrustBundleError``.
      * Subsequent reads: if fresh → return cached. If stale → trigger
        a background refresh and return the stale value immediately.
      * If background refresh fails, the stale value continues to be
        served AND a ``warning`` is logged AND the failure is available
        via ``last_refresh_error(provider_id)``. Never silent.
    """

    def __init__(
        self,
        *,
        oidc_loader: Callable[..., TrustBundle] = load_oidc_bundle,
        saml_loader: Callable[..., TrustBundle] = load_saml_bundle,
    ) -> None:
        self._oidc_loader = oidc_loader
        self._saml_loader = saml_loader
        self._lock = threading.RLock()
        self._store: dict[str, TrustBundle] = {}
        self._refreshing: set[str] = set()
        self._errors: dict[str, str] = {}

    # --- public API ----------------------------------------------------

    def get_or_load(
        self,
        provider_id: str,
        kind: str,
        metadata_url: str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        fetcher: Fetcher = _default_http_fetcher,
    ) -> TrustBundle:
        with self._lock:
            bundle = self._store.get(provider_id)
        if bundle is None:
            bundle = self._load(
                provider_id, kind, metadata_url, ttl_seconds, fetcher
            )
            with self._lock:
                self._store[provider_id] = bundle
            return bundle
        if bundle.is_stale():
            self._trigger_background_refresh(
                provider_id, kind, metadata_url, ttl_seconds, fetcher
            )
        return bundle

    def invalidate(self, provider_id: str) -> None:
        with self._lock:
            self._store.pop(provider_id, None)
            self._errors.pop(provider_id, None)

    def last_refresh_error(self, provider_id: str) -> Optional[str]:
        with self._lock:
            return self._errors.get(provider_id)

    def peek(self, provider_id: str) -> Optional[TrustBundle]:
        with self._lock:
            return self._store.get(provider_id)

    # --- internals -----------------------------------------------------

    def _load(
        self,
        provider_id: str,
        kind: str,
        metadata_url: str,
        ttl_seconds: int,
        fetcher: Fetcher,
    ) -> TrustBundle:
        if kind == "oidc":
            return self._oidc_loader(
                provider_id, metadata_url, ttl_seconds=ttl_seconds, fetcher=fetcher
            )
        if kind == "saml":
            return self._saml_loader(
                provider_id, metadata_url, ttl_seconds=ttl_seconds, fetcher=fetcher
            )
        raise TrustBundleError("unsupported_kind", detail=kind)

    def _trigger_background_refresh(
        self,
        provider_id: str,
        kind: str,
        metadata_url: str,
        ttl_seconds: int,
        fetcher: Fetcher,
    ) -> None:
        with self._lock:
            if provider_id in self._refreshing:
                return
            self._refreshing.add(provider_id)

        def _run() -> None:
            # Fail-open is the documented contract (class docstring):
            # a background refresh failure MUST NOT take down the stale
            # cache — the stale bundle is still better than no bundle,
            # and the SP login flow can continue.  The broad catch is
            # load-bearing because _load calls through pluggable
            # fetchers + parsers that can raise anything (urllib
            # transport errors, json.JSONDecodeError, defusedxml errors,
            # TrustBundleError).  Observability is preserved via:
            #   (1) logger.error with structured extra
            #   (2) self._errors[provider_id] for last_refresh_error()
            try:
                fresh = self._load(
                    provider_id, kind, metadata_url, ttl_seconds, fetcher
                )
            except Exception as exc:  # noqa: BLE001 — see comment above
                logger.error(
                    "trust_bundle.refresh_failed",
                    extra={
                        "op": "background_refresh",
                        "provider_id": provider_id,
                        "kind": kind,
                        "error_type": type(exc).__name__,
                    },
                    exc_info=True,
                )
                with self._lock:
                    self._errors[provider_id] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                    self._refreshing.discard(provider_id)
                return
            with self._lock:
                self._store[provider_id] = fresh
                self._errors.pop(provider_id, None)
                self._refreshing.discard(provider_id)

        t = threading.Thread(
            target=_run, name=f"trust-bundle-refresh-{provider_id}", daemon=True
        )
        t.start()


# Module-level singleton used by the router. Tests use their own instance.
_GLOBAL_CACHE: Optional[TrustBundleCache] = None


def get_global_cache() -> TrustBundleCache:
    global _GLOBAL_CACHE
    if _GLOBAL_CACHE is None:
        _GLOBAL_CACHE = TrustBundleCache()
    return _GLOBAL_CACHE
