"""SS-31 slice C — SP-side SAML 2.0 helpers.

Scope (per SS-31 prompt):
  * Build an AuthnRequest (SP-initiated SSO, HTTP-Redirect binding).
  * Parse an IdP SAML Response / Assertion using ``defusedxml`` ONLY.
    ``xml.etree`` is NEVER imported here — that would re-open the door
    to XXE and billion-laughs on untrusted IdP payloads.
  * Validate the assertion against the provider's trust bundle:
    issuer match, NotBefore / NotOnOrAfter window, Audience restriction,
    and — crucially — that a signing cert from the trust bundle is
    present on the Response or Assertion.

Deliberate non-goals for this slice:
  * Full XML DSig canonicalisation + signature verification in pure
    Python (unsafe to roll by hand; left as an TODO with a
    hard gate below — the validator refuses to return a "valid" verdict
    without it unless the caller passes ``_unsafe_skip_xmldsig=True``,
    which is used only by the unit tests under a loud warning).
  * SAML encryption. Per SS-31 plan: assertions are transport-secured
    via HTTPS on the ACS endpoint.
"""
from __future__ import annotations

import base64
import logging
import secrets
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from defusedxml import ElementTree as DefusedET  # type: ignore

from gdx_dispatch.core.federation.trust_bundle import TrustBundle, TrustBundleError

logger = logging.getLogger(__name__)

SAML_NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}


class SAMLError(Exception):
    def __init__(self, reason: str, *, detail: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------------------
# AuthnRequest builder
# ---------------------------------------------------------------------------


@dataclass
class AuthnRequestContext:
    """Echoed back by the IdP via the Response's InResponseTo. The
    router persists this keyed by ``id`` and replays it on ACS."""

    id: str
    issued_at: datetime
    relay_state: str


def build_authn_request(
    *,
    sp_entity_id: str,
    acs_url: str,
    idp_sso_url: str,
) -> tuple[str, AuthnRequestContext]:
    """Return ``(redirect_url, context)``.

    Uses HTTP-Redirect binding: the AuthnRequest is deflated + base64 +
    URL-encoded per SAML binding spec. No signature on the request for
    v1 (most enterprise IdPs allow unsigned AuthnRequests from a
    pre-registered SP).
    """
    req_id = "_" + uuid.uuid4().hex
    issued_at = datetime.now(timezone.utc).replace(microsecond=0)
    relay_state = secrets.token_urlsafe(24)

    xml = (
        '<samlp:AuthnRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'ID="{req_id}" Version="2.0" '
        f'IssueInstant="{issued_at.strftime("%Y-%m-%dT%H:%M:%SZ")}" '
        f'Destination="{_xml_attr(idp_sso_url)}" '
        'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'AssertionConsumerServiceURL="{_xml_attr(acs_url)}">'
        f"<saml:Issuer>{_xml_text(sp_entity_id)}</saml:Issuer>"
        '<samlp:NameIDPolicy Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress" '
        'AllowCreate="true"/>'
        "</samlp:AuthnRequest>"
    )

    deflated = zlib.compress(xml.encode("utf-8"))[2:-4]  # raw deflate
    encoded = base64.b64encode(deflated).decode("ascii")
    qs = urlencode({"SAMLRequest": encoded, "RelayState": relay_state})
    url = f"{idp_sso_url}{'&' if '?' in idp_sso_url else '?'}{qs}"
    ctx = AuthnRequestContext(id=req_id, issued_at=issued_at, relay_state=relay_state)
    return url, ctx


def _xml_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _xml_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;")


# ---------------------------------------------------------------------------
# Response / assertion parsing
# ---------------------------------------------------------------------------


@dataclass
class ParsedAssertion:
    issuer: str
    subject: str
    attributes: dict[str, list[str]]
    not_before: Optional[datetime]
    not_on_or_after: Optional[datetime]
    audiences: list[str]
    in_response_to: Optional[str]
    signing_cert_b64: Optional[str]


def parse_saml_response(b64_response: str) -> ParsedAssertion:
    """Decode + parse a base64 SAML Response using ``defusedxml``.

    Does NOT validate — returns a structured view. Call
    ``validate_assertion`` next.
    """
    try:
        xml_bytes = base64.b64decode(b64_response)
    except Exception as exc:  # noqa: BLE001
        raise SAMLError("malformed_response", detail=str(exc)) from exc

    try:
        root = DefusedET.fromstring(xml_bytes)
    except Exception as exc:  # noqa: BLE001
        raise SAMLError("malformed_response", detail=str(exc)) from exc

    # Response or Assertion may be the root depending on IdP. Find the
    # first Assertion node — also supports IdP-signed-Response wrapping.
    assertion = root.find(".//saml:Assertion", SAML_NS)
    if assertion is None and root.tag.endswith("}Assertion"):
        assertion = root
    if assertion is None:
        raise SAMLError("no_assertion")

    issuer_el = assertion.find("saml:Issuer", SAML_NS)
    if issuer_el is None or not (issuer_el.text or "").strip():
        raise SAMLError("no_issuer")
    issuer = issuer_el.text.strip()

    subj_el = assertion.find("saml:Subject/saml:NameID", SAML_NS)
    if subj_el is None or not (subj_el.text or "").strip():
        raise SAMLError("no_subject")
    subject = subj_el.text.strip()

    # Conditions
    not_before = None
    not_on_or_after = None
    audiences: list[str] = []
    cond = assertion.find("saml:Conditions", SAML_NS)
    if cond is not None:
        if cond.attrib.get("NotBefore"):
            not_before = _parse_iso(cond.attrib["NotBefore"])
        if cond.attrib.get("NotOnOrAfter"):
            not_on_or_after = _parse_iso(cond.attrib["NotOnOrAfter"])
        for a in cond.findall(
            "saml:AudienceRestriction/saml:Audience", SAML_NS
        ):
            if a.text:
                audiences.append(a.text.strip())

    # Attributes
    attrs: dict[str, list[str]] = {}
    for a in assertion.findall(
        "saml:AttributeStatement/saml:Attribute", SAML_NS
    ):
        name = a.attrib.get("Name") or a.attrib.get("FriendlyName")
        if not name:
            continue
        values = [
            (v.text or "").strip()
            for v in a.findall("saml:AttributeValue", SAML_NS)
        ]
        attrs[name] = values

    # InResponseTo (on Response root) or SubjectConfirmationData
    in_response_to = root.attrib.get("InResponseTo")
    if not in_response_to:
        scd = assertion.find(
            "saml:Subject/saml:SubjectConfirmation/saml:SubjectConfirmationData",
            SAML_NS,
        )
        if scd is not None:
            in_response_to = scd.attrib.get("InResponseTo")

    # Signing cert — look on the Response or Assertion's ds:Signature.
    signing_cert_b64 = None
    sig = root.find(".//ds:Signature", SAML_NS)
    if sig is not None:
        cert_el = sig.find(".//ds:X509Certificate", SAML_NS)
        if cert_el is not None and cert_el.text:
            signing_cert_b64 = "".join(cert_el.text.split())

    return ParsedAssertion(
        issuer=issuer,
        subject=subject,
        attributes=attrs,
        not_before=not_before,
        not_on_or_after=not_on_or_after,
        audiences=audiences,
        in_response_to=in_response_to,
        signing_cert_b64=signing_cert_b64,
    )


def _parse_iso(s: str) -> datetime:
    # SAML uses ISO 8601 with "Z" — normalise to tz-aware UTC.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_assertion(
    parsed: ParsedAssertion,
    *,
    bundle: TrustBundle,
    expected_audience: str,
    expected_in_response_to: Optional[str],
    now: Optional[datetime] = None,
    clock_skew_seconds: int = 60,
    _unsafe_skip_xmldsig: bool = False,
) -> None:
    """Raise ``SAMLError`` on any failure. No return value on success —
    callers get the data out of ``parsed``.
    """
    if bundle.kind != "saml":
        raise SAMLError("not_saml_bundle")
    if parsed.issuer != bundle.issuer:
        raise SAMLError("issuer_mismatch", detail=parsed.issuer)

    now_dt = now or datetime.now(timezone.utc)
    skew = _skew(clock_skew_seconds)
    if parsed.not_before and now_dt + skew < parsed.not_before:
        raise SAMLError("not_yet_valid")
    if parsed.not_on_or_after and now_dt - skew >= parsed.not_on_or_after:
        raise SAMLError("assertion_expired")
    if parsed.audiences and expected_audience not in parsed.audiences:
        raise SAMLError("audience_mismatch")

    if expected_in_response_to is not None and parsed.in_response_to != expected_in_response_to:
        raise SAMLError("in_response_to_mismatch")

    # signature verification (canonicalisation + SignedInfo hash +
    # SignatureValue check against bundle.signing_certs_pem). Rolling
    # that from scratch is unsafe, so we require a signing cert to be
    # present AND match one from the trust bundle as a minimum bar, and
    # refuse to succeed silently without xmldsig.
    if _unsafe_skip_xmldsig:
        logger.warning(
            "SAML xmldsig verification SKIPPED for provider=%s — unit-test path only",
            bundle.provider_id,
        )
        return

    if not parsed.signing_cert_b64:
        raise SAMLError("assertion_unsigned")
    bundle_certs_b64 = {
        _pem_body(c) for c in bundle.signing_certs_pem
    }
    if parsed.signing_cert_b64 not in bundle_certs_b64:
        raise SAMLError(
            "signing_cert_not_trusted",
            detail="assertion signing cert not in trust bundle",
        )
    # still raise on bundle-cert mismatch so a forged assertion signed
    # with a random cert is rejected.
    raise SAMLError(
        "xmldsig_not_implemented",
        detail="TODO: XMLDSig verification required before production use",
    )


def _pem_body(pem: str) -> str:
    return "".join(
        line
        for line in pem.splitlines()
        if line and not line.startswith("-----")
    )


def _skew(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Profile shaping
# ---------------------------------------------------------------------------


_ATTR_EMAIL_KEYS = (
    "email",
    "mail",
    "urn:oid:0.9.2342.19200300.100.1.3",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
)


def assertion_to_profile(parsed: ParsedAssertion) -> dict[str, Any]:
    """Normalise attrs to the ``reconcile_federated_identity`` shape."""

    def _first(*keys: str) -> Optional[str]:
        for k in keys:
            vals = parsed.attributes.get(k) or []
            if vals:
                return vals[0]
        return None

    email = _first(*_ATTR_EMAIL_KEYS)
    name = _first(
        "name",
        "displayName",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    )
    given = _first(
        "givenName",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname",
    )
    family = _first(
        "sn",
        "surname",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname",
    )
    return {
        "external_subject": parsed.subject,
        "email": email,
        "email_verified": bool(email),  # IdP attests via SAML binding
        "name": name or (f"{given or ''} {family or ''}".strip() or None),
        "given_name": given,
        "family_name": family,
        "preferred_username": _first("uid", "sAMAccountName") or parsed.subject,
    }
