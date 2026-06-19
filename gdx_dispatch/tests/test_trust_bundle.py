"""SS-31 slice A — trust bundle loader + TTL cache tests."""
from __future__ import annotations

import json
import time

import pytest

from gdx_dispatch.core.federation import trust_bundle as tb


# ---------------------------------------------------------------------------
# OIDC
# ---------------------------------------------------------------------------


def _oidc_docs():
    meta = {
        "issuer": "https://idp.example.com/",
        "jwks_uri": "https://idp.example.com/jwks",
        "authorization_endpoint": "https://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
    }
    jwks = {"keys": [{"kty": "RSA", "kid": "k1", "use": "sig", "n": "x", "e": "AQAB"}]}
    return meta, jwks


def _make_oidc_fetcher(meta, jwks):
    def _fetch(url):
        if "jwks" in url:
            return json.dumps(jwks).encode()
        return json.dumps(meta).encode()

    return _fetch


def test_load_oidc_bundle_happy():
    meta, jwks = _oidc_docs()
    bundle = tb.load_oidc_bundle(
        "p1", "https://idp.example.com/.well-known/openid-configuration",
        fetcher=_make_oidc_fetcher(meta, jwks),
    )
    assert bundle.kind == "oidc"
    assert bundle.issuer == "https://idp.example.com/"
    assert bundle.authorization_endpoint == meta["authorization_endpoint"]
    assert bundle.jwks["keys"][0]["kid"] == "k1"


def test_load_oidc_bundle_missing_fields_raises():
    meta, jwks = _oidc_docs()
    meta.pop("token_endpoint")
    with pytest.raises(tb.TrustBundleError) as ei:
        tb.load_oidc_bundle(
            "p1", "https://idp/x", fetcher=_make_oidc_fetcher(meta, jwks)
        )
    assert ei.value.reason == "invalid_metadata"


def test_load_oidc_bundle_empty_jwks_raises():
    meta, _ = _oidc_docs()
    jwks = {"keys": []}
    with pytest.raises(tb.TrustBundleError) as ei:
        tb.load_oidc_bundle(
            "p1", "https://idp/x", fetcher=_make_oidc_fetcher(meta, jwks)
        )
    assert ei.value.reason == "invalid_jwks"


# ---------------------------------------------------------------------------
# SAML
# ---------------------------------------------------------------------------


SAML_METADATA_XML = b"""<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     xmlns:ds="http://www.w3.org/2000/09/xmldsig#"
                     entityID="https://idp.example.com/saml">
  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:KeyDescriptor use="signing">
      <ds:KeyInfo>
        <ds:X509Data>
          <ds:X509Certificate>MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA</ds:X509Certificate>
        </ds:X509Data>
      </ds:KeyInfo>
    </md:KeyDescriptor>
    <md:SingleSignOnService
      Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
      Location="https://idp.example.com/saml/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>
"""


def test_load_saml_bundle_happy():
    bundle = tb.load_saml_bundle(
        "p2", "https://idp/metadata", fetcher=lambda _url: SAML_METADATA_XML
    )
    assert bundle.kind == "saml"
    assert bundle.issuer == "https://idp.example.com/saml"
    assert bundle.sso_endpoint == "https://idp.example.com/saml/sso"
    assert bundle.signing_certs_pem
    assert "BEGIN CERTIFICATE" in bundle.signing_certs_pem[0]


def test_load_saml_bundle_rejects_missing_cert():
    no_cert = SAML_METADATA_XML.replace(
        b"<ds:X509Certificate>MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA</ds:X509Certificate>",
        b"",
    )
    with pytest.raises(tb.TrustBundleError) as ei:
        tb.load_saml_bundle("p2", "u", fetcher=lambda _u: no_cert)
    assert ei.value.reason == "invalid_metadata"


def test_load_saml_bundle_rejects_xxe_like_malformed():
    with pytest.raises(tb.TrustBundleError):
        tb.load_saml_bundle("p2", "u", fetcher=lambda _u: b"<<<not xml>>>")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_cache_serves_fresh_without_reload():
    meta, jwks = _oidc_docs()
    calls = {"n": 0}

    def _fetch(url):
        calls["n"] += 1
        return (json.dumps(jwks) if "jwks" in url else json.dumps(meta)).encode()

    cache = tb.TrustBundleCache()
    b1 = cache.get_or_load("p", "oidc", "u", ttl_seconds=3600, fetcher=_fetch)
    b2 = cache.get_or_load("p", "oidc", "u", ttl_seconds=3600, fetcher=_fetch)
    assert b1 is b2
    # initial load = 2 fetches (meta + jwks). No reload on the second call.
    assert calls["n"] == 2


def test_cache_serves_stale_when_refresh_fails():
    meta, jwks = _oidc_docs()
    state = {"fail": False}

    def _fetch(url):
        if state["fail"]:
            raise tb.TrustBundleError("fetch_failed", detail="down")
        return (json.dumps(jwks) if "jwks" in url else json.dumps(meta)).encode()

    cache = tb.TrustBundleCache()
    b = cache.get_or_load("p", "oidc", "u", ttl_seconds=0, fetcher=_fetch)
    # force stale + make fetch fail on refresh
    state["fail"] = True
    b2 = cache.get_or_load("p", "oidc", "u", ttl_seconds=0, fetcher=_fetch)
    assert b2 is b  # stale served
    # wait for background refresh to log the error
    deadline = time.time() + 2.0
    while time.time() < deadline and cache.last_refresh_error("p") is None:
        time.sleep(0.02)
    assert cache.last_refresh_error("p") is not None


def test_cache_first_load_failure_raises():
    def _fetch(_url):
        raise tb.TrustBundleError("fetch_failed", detail="boom")

    cache = tb.TrustBundleCache()
    with pytest.raises(tb.TrustBundleError):
        cache.get_or_load("p", "oidc", "u", fetcher=_fetch)


def test_cache_unsupported_kind():
    cache = tb.TrustBundleCache()
    with pytest.raises(tb.TrustBundleError) as ei:
        cache.get_or_load("p", "ldap", "u", fetcher=lambda _u: b"{}")
    assert ei.value.reason == "unsupported_kind"
