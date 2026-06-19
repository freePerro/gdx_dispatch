"""SS-31 slice C — SAML provider tests."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest

from gdx_dispatch.core.federation import saml_provider as sp
from gdx_dispatch.core.federation.trust_bundle import TrustBundle


DUMMY_CERT_B64 = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA"


def _assertion_xml(
    *,
    issuer="https://idp.example.com/saml",
    audience="gdx-sp",
    subject="user@example.com",
    not_before=None,
    not_on_or_after=None,
    in_response_to="_req-123",
    include_signature=True,
):
    nb = (not_before or (datetime.now(timezone.utc) - timedelta(minutes=5))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    na = (
        not_on_or_after or (datetime.now(timezone.utc) + timedelta(minutes=5))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    sig_xml = (
        f'<ds:Signature xmlns:ds="http://www.w3.org/2000/09/xmldsig#">'
        f"<ds:KeyInfo><ds:X509Data><ds:X509Certificate>{DUMMY_CERT_B64}"
        f"</ds:X509Certificate></ds:X509Data></ds:KeyInfo></ds:Signature>"
        if include_signature
        else ""
    )
    return (
        f'<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        f'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'InResponseTo="{in_response_to}">'
        f'<saml:Assertion Version="2.0" IssueInstant="{nb}">'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        f"<saml:Subject><saml:NameID>{subject}</saml:NameID>"
        f"<saml:SubjectConfirmation>"
        f'<saml:SubjectConfirmationData InResponseTo="{in_response_to}"/>'
        f"</saml:SubjectConfirmation></saml:Subject>"
        f'<saml:Conditions NotBefore="{nb}" NotOnOrAfter="{na}">'
        f"<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience>"
        f"</saml:AudienceRestriction></saml:Conditions>"
        f"<saml:AttributeStatement>"
        f'<saml:Attribute Name="email"><saml:AttributeValue>{subject}'
        f"</saml:AttributeValue></saml:Attribute>"
        f'<saml:Attribute Name="givenName"><saml:AttributeValue>Ada'
        f"</saml:AttributeValue></saml:Attribute>"
        f'<saml:Attribute Name="sn"><saml:AttributeValue>Lovelace'
        f"</saml:AttributeValue></saml:Attribute>"
        f"</saml:AttributeStatement>"
        f"{sig_xml}"
        f"</saml:Assertion></samlp:Response>"
    )


def _b64(xml: str) -> str:
    return base64.b64encode(xml.encode()).decode()


def _bundle(cert_b64=DUMMY_CERT_B64, issuer="https://idp.example.com/saml"):
    pem = (
        "-----BEGIN CERTIFICATE-----\n"
        + cert_b64
        + "\n-----END CERTIFICATE-----\n"
    )
    return TrustBundle(
        provider_id="p",
        kind="saml",
        issuer=issuer,
        sso_endpoint="https://idp.example.com/saml/sso",
        signing_certs_pem=[pem],
    )


# ---------------------------------------------------------------------------


def test_build_authn_request_produces_url_and_context():
    url, ctx = sp.build_authn_request(
        sp_entity_id="gdx-sp", acs_url="https://sp/acs", idp_sso_url="https://idp/sso"
    )
    assert url.startswith("https://idp/sso?")
    assert "SAMLRequest=" in url and "RelayState=" in url
    assert ctx.id.startswith("_") and len(ctx.relay_state) > 10


def test_parse_saml_response_extracts_fields():
    xml = _assertion_xml()
    parsed = sp.parse_saml_response(_b64(xml))
    assert parsed.issuer == "https://idp.example.com/saml"
    assert parsed.subject == "user@example.com"
    assert "gdx-sp" in parsed.audiences
    assert parsed.in_response_to == "_req-123"
    assert parsed.attributes["email"] == ["user@example.com"]
    assert parsed.signing_cert_b64 == DUMMY_CERT_B64


def test_parse_saml_response_rejects_malformed():
    with pytest.raises(sp.SAMLError):
        sp.parse_saml_response("not-base64!@#")
    with pytest.raises(sp.SAMLError):
        sp.parse_saml_response(base64.b64encode(b"<<<nope").decode())


def test_validate_assertion_happy_path_with_unsafe_skip():
    parsed = sp.parse_saml_response(_b64(_assertion_xml()))
    sp.validate_assertion(
        parsed,
        bundle=_bundle(),
        expected_audience="gdx-sp",
        expected_in_response_to="_req-123",
        _unsafe_skip_xmldsig=True,
    )


def test_validate_assertion_issuer_mismatch():
    parsed = sp.parse_saml_response(_b64(_assertion_xml(issuer="https://evil/")))
    with pytest.raises(sp.SAMLError) as ei:
        sp.validate_assertion(
            parsed, bundle=_bundle(), expected_audience="gdx-sp",
            expected_in_response_to="_req-123", _unsafe_skip_xmldsig=True,
        )
    assert ei.value.reason == "issuer_mismatch"


def test_validate_assertion_audience_mismatch():
    parsed = sp.parse_saml_response(_b64(_assertion_xml(audience="wrong")))
    with pytest.raises(sp.SAMLError) as ei:
        sp.validate_assertion(
            parsed, bundle=_bundle(), expected_audience="gdx-sp",
            expected_in_response_to="_req-123", _unsafe_skip_xmldsig=True,
        )
    assert ei.value.reason == "audience_mismatch"


def test_validate_assertion_expired():
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    parsed = sp.parse_saml_response(
        _b64(_assertion_xml(not_before=past - timedelta(minutes=5), not_on_or_after=past))
    )
    with pytest.raises(sp.SAMLError) as ei:
        sp.validate_assertion(
            parsed, bundle=_bundle(), expected_audience="gdx-sp",
            expected_in_response_to="_req-123", _unsafe_skip_xmldsig=True,
        )
    assert ei.value.reason == "assertion_expired"


def test_validate_assertion_in_response_to_mismatch():
    parsed = sp.parse_saml_response(_b64(_assertion_xml(in_response_to="_other")))
    with pytest.raises(sp.SAMLError) as ei:
        sp.validate_assertion(
            parsed, bundle=_bundle(), expected_audience="gdx-sp",
            expected_in_response_to="_req-123", _unsafe_skip_xmldsig=True,
        )
    assert ei.value.reason == "in_response_to_mismatch"


def test_validate_assertion_unsigned_rejected():
    parsed = sp.parse_saml_response(_b64(_assertion_xml(include_signature=False)))
    with pytest.raises(sp.SAMLError) as ei:
        sp.validate_assertion(
            parsed, bundle=_bundle(), expected_audience="gdx-sp",
            expected_in_response_to="_req-123",
        )
    assert ei.value.reason == "assertion_unsigned"


def test_validate_assertion_cert_not_in_trust_bundle():
    parsed = sp.parse_saml_response(_b64(_assertion_xml()))
    # Trust bundle has a DIFFERENT cert
    bad = _bundle(cert_b64="OTHERCERTBYTES")
    with pytest.raises(sp.SAMLError) as ei:
        sp.validate_assertion(
            parsed, bundle=bad, expected_audience="gdx-sp",
            expected_in_response_to="_req-123",
        )
    assert ei.value.reason == "signing_cert_not_trusted"


def test_validate_assertion_xmldsig_not_implemented_gate():
    """Without _unsafe_skip_xmldsig, the validator refuses to succeed
    silently — it raises xmldsig_not_implemented as a LOUD integration
    gate."""
    parsed = sp.parse_saml_response(_b64(_assertion_xml()))
    with pytest.raises(sp.SAMLError) as ei:
        sp.validate_assertion(
            parsed, bundle=_bundle(), expected_audience="gdx-sp",
            expected_in_response_to="_req-123",
        )
    assert ei.value.reason == "xmldsig_not_implemented"


def test_assertion_to_profile_normalises_attrs():
    parsed = sp.parse_saml_response(_b64(_assertion_xml()))
    prof = sp.assertion_to_profile(parsed)
    assert prof["external_subject"] == "user@example.com"
    assert prof["email"] == "user@example.com"
    assert prof["given_name"] == "Ada"
    assert prof["family_name"] == "Lovelace"


def test_parse_saml_rejects_xxe_entity():
    # defusedxml should refuse this outright.
    malicious = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE r [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">&e;</samlp:Response>'
    )
    with pytest.raises(sp.SAMLError):
        sp.parse_saml_response(_b64(malicious))
