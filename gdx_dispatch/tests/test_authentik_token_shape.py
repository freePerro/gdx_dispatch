"""SS-6 Slice A + Slice C — Authentik provider token-shape tests.

Deterministic unit tests for the ``gdx_tid`` property mapping and the
synthetic provider token payloads (``gdx-spa``, ``gdx-thirdparty``).
These tests do NOT hit a live Authentik instance — they validate the
pure-Python mapping function and the provider-payload helpers that
``configure_authentik.py`` would send to the Authentik admin API.

Covered:

* ``build_gdx_tid_claims`` honors the D-5 singular-tenant contract
  (``gdx_tid`` only; no ``tenants[]`` array).
* Fail-closed semantics when ``memberships`` is missing/empty or when
  ``active_tenant`` is not a valid member.
* D18 assumption: ``ASSUMED_IDENTITY_TYPE == "human"`` and no
  ``identity_type`` claim is emitted in Slice A.
* SPA provider payload (``build_gdx_spa_provider_payload``) enforces
  OAuth 2.1 strict values (PKCE required, no implicit flow, fixed
  redirect URI, audience ``gdx-api``, 15-minute access tokens).
* Third-party provider payload (``build_gdx_thirdparty_provider_payload``)
  enforces the Slice C contract (PKCE required, auth-code only, fixed
  redirect URI, audience ``gdx-api``, 1-hour access / 90-day refresh)
  and keeps client_id / signing key / redirect URI / TTL distinct from
  the SPA provider.
* Sandbox expression is syntactically valid Python.
"""
from __future__ import annotations

import ast
import time
import uuid
from typing import Any

import pytest

from gdx_dispatch.tools.authentik_property_mapping_gdx_tid import (
    ASSUMED_IDENTITY_TYPE,
    CLAIM_SCOPE_NAME,
    SANDBOX_EXPRESSION,
    build_gdx_tid_claims,
)
from gdx_dispatch.tools.configure_authentik import (
    SUPPORTED_PROVIDERS,
    GDX_SPA_ACCESS_TOKEN_VALIDITY,
    GDX_SPA_AUDIENCE,
    GDX_SPA_PROVIDER_NAME,
    GDX_SPA_REDIRECT_URI,
    GDX_THIRDPARTY_ACCESS_TOKEN_VALIDITY,
    GDX_THIRDPARTY_AUDIENCE,
    GDX_THIRDPARTY_PROVIDER_NAME,
    GDX_THIRDPARTY_REDIRECT_URI,
    GDX_THIRDPARTY_REFRESH_TOKEN_VALIDITY,
    build_gdx_spa_provider_payload,
    build_gdx_thirdparty_provider_payload,
)

D5_REQUIRED_CLAIMS = {"iss", "aud", "sub", "exp", "iat", CLAIM_SCOPE_NAME}
D5_FORBIDDEN_CLAIMS = {"tenants", "tenants_array", "tid_list"}
SPA_ACCESS_TTL_SECONDS = 15 * 60
THIRDPARTY_ACCESS_TTL_SECONDS = 60 * 60
THIRDPARTY_REFRESH_TTL_DAYS = 90


def _synthesize_spa_access_token(
    user_attributes: dict[str, Any],
    *,
    sub: str,
    now: int | None = None,
    iss: str = f"https://auth.example.com/application/o/{GDX_SPA_PROVIDER_NAME}/",
) -> dict[str, Any]:
    """Build the payload Authentik would mint for a gdx-spa access token.

    This is the deterministic test surrogate for the real token mint: it
    calls the same property-mapping function Authentik will run in its
    sandbox, and applies the provider-level claims
    (iss/aud/sub/iat/exp) that Authentik fills in from provider config.
    """
    now = now if now is not None else int(time.time())
    scope_claims = build_gdx_tid_claims(user_attributes)
    return {
        "iss": iss,
        "aud": GDX_SPA_AUDIENCE,
        "sub": sub,
        "iat": now,
        "exp": now + SPA_ACCESS_TTL_SECONDS,
        CLAIM_SCOPE_NAME: scope_claims[CLAIM_SCOPE_NAME],
    }


def _synthesize_thirdparty_access_token(
    user_attributes: dict[str, Any],
    *,
    sub: str,
    now: int | None = None,
    iss: str = f"https://auth.example.com/application/o/{GDX_THIRDPARTY_PROVIDER_NAME}/",
) -> dict[str, Any]:
    """Build the payload Authentik would mint for a gdx-thirdparty access token.

    Same shape contract as the SPA surrogate — the two providers share
    audience (``gdx-api``) and the ``gdx_tid`` scope, and SS-7 validates
    both via the same JWKS-backed pipeline. Only the issuer URL and the
    1-hour TTL differ from the SPA mint.
    """
    now = now if now is not None else int(time.time())
    scope_claims = build_gdx_tid_claims(user_attributes)
    return {
        "iss": iss,
        "aud": GDX_THIRDPARTY_AUDIENCE,
        "sub": sub,
        "iat": now,
        "exp": now + THIRDPARTY_ACCESS_TTL_SECONDS,
        CLAIM_SCOPE_NAME: scope_claims[CLAIM_SCOPE_NAME],
    }


# ---------------------------------------------------------------------------
# Property mapping — claim shape
# ---------------------------------------------------------------------------


def test_claim_scope_name_is_gdx_tid():
    assert CLAIM_SCOPE_NAME == "gdx_tid"


def test_mapping_returns_active_tenant_when_explicit():
    attrs = {"memberships": ["gdx", "acme"], "active_tenant": "acme"}
    assert build_gdx_tid_claims(attrs) == {"gdx_tid": "acme"}


def test_mapping_defaults_to_first_membership_when_active_tenant_missing():
    attrs = {"memberships": ["gdx", "acme"]}
    assert build_gdx_tid_claims(attrs) == {"gdx_tid": "gdx"}


def test_mapping_emits_only_gdx_tid_no_identity_type_in_slice_a():
    # D-5 + D18: Slice A emits a single-key claim dict. identity_type
    # will land when D18 hardens; until then the mapping must not emit it.
    attrs = {"memberships": ["gdx"], "active_tenant": "gdx"}
    claims = build_gdx_tid_claims(attrs)
    assert set(claims.keys()) == {"gdx_tid"}


# ---------------------------------------------------------------------------
# Property mapping — fail-closed behavior
# ---------------------------------------------------------------------------


def test_mapping_fails_closed_when_memberships_missing():
    with pytest.raises(Exception, match="memberships"):
        build_gdx_tid_claims({"active_tenant": "gdx"})


def test_mapping_fails_closed_when_memberships_empty():
    with pytest.raises(Exception, match="memberships"):
        build_gdx_tid_claims({"memberships": [], "active_tenant": "gdx"})


def test_mapping_fails_closed_when_memberships_not_a_list():
    with pytest.raises(Exception, match="memberships"):
        build_gdx_tid_claims({"memberships": "gdx", "active_tenant": "gdx"})


def test_mapping_rejects_active_tenant_not_in_memberships():
    with pytest.raises(Exception, match="active_tenant"):
        build_gdx_tid_claims({"memberships": ["gdx"], "active_tenant": "attacker"})


# ---------------------------------------------------------------------------
# D18 assumption
# ---------------------------------------------------------------------------


def test_d18_assumed_identity_type_is_human():
    # Slice A assumption: every Authentik-linked identity is "human" until
    # Identity.type lands in the platform schema. The constant is the
    # single source of truth so SS-7 can read it when it starts enforcing.
    assert ASSUMED_IDENTITY_TYPE == "human"


# ---------------------------------------------------------------------------
# Sandbox expression
# ---------------------------------------------------------------------------


def test_sandbox_expression_is_syntactically_valid_python_body():
    # Authentik wraps the expression body in an outer ``def mapping(user, ...):``.
    # Parsing the indented body as a function body is a cheap drift-guard:
    # if someone breaks syntax (unbalanced parens, stray ``import``), this
    # fails loud long before a token mint does.
    wrapped = "def _mapping(user, request, db_session):\n" + "\n".join(
        "    " + line for line in SANDBOX_EXPRESSION.splitlines()
    )
    ast.parse(wrapped)


def test_sandbox_expression_returns_gdx_tid_claim():
    assert "return {\"gdx_tid\": active_tenant}" in SANDBOX_EXPRESSION


def test_sandbox_expression_has_no_imports():
    # Authentik's sandbox restricts imports. If one slipped in, token mint
    # would fail at runtime; catch it here instead.
    for line in SANDBOX_EXPRESSION.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("import "), stripped
        assert not stripped.startswith("from "), stripped


# ---------------------------------------------------------------------------
# SPA provider payload — OAuth 2.1 / PKCE contract
# ---------------------------------------------------------------------------


def test_provider_payload_enforces_pkce_s256_required():
    payload = build_gdx_spa_provider_payload(
        signing_key_pk="sk", property_mapping_pks=["m"], authorization_flow_pk="f"
    )
    assert payload["pkce_mode"] == "required"


def test_provider_payload_forbids_implicit_flow():
    payload = build_gdx_spa_provider_payload(
        signing_key_pk="sk", property_mapping_pks=["m"], authorization_flow_pk="f"
    )
    assert payload["response_types"] == ["code"]
    assert "token" not in payload["response_types"]
    assert "id_token" not in payload["response_types"]


def test_provider_payload_uses_fixed_redirect_uri():
    payload = build_gdx_spa_provider_payload(
        signing_key_pk="sk", property_mapping_pks=["m"], authorization_flow_pk="f"
    )
    assert payload["redirect_uris"] == GDX_SPA_REDIRECT_URI
    assert "*" not in payload["redirect_uris"]


def test_provider_payload_audience_is_gdx_api():
    payload = build_gdx_spa_provider_payload(
        signing_key_pk="sk", property_mapping_pks=["m"], authorization_flow_pk="f"
    )
    assert payload["audience"] == GDX_SPA_AUDIENCE == "gdx-api"


def test_provider_payload_access_token_validity_is_15_minutes():
    payload = build_gdx_spa_provider_payload(
        signing_key_pk="sk", property_mapping_pks=["m"], authorization_flow_pk="f"
    )
    assert GDX_SPA_ACCESS_TOKEN_VALIDITY == "minutes=15"
    assert payload["access_token_validity"] == "minutes=15"


# ---------------------------------------------------------------------------
# Synthesized SPA access token shape
# ---------------------------------------------------------------------------


def test_spa_token_payload_has_all_d5_required_claims():
    attrs = {"memberships": ["gdx"], "active_tenant": "gdx"}
    payload = _synthesize_spa_access_token(attrs, sub=str(uuid.uuid4()))
    missing = D5_REQUIRED_CLAIMS - payload.keys()
    assert not missing, f"payload missing required D-5 claims: {sorted(missing)}"


def test_spa_token_payload_carries_singular_gdx_tid():
    attrs = {"memberships": ["gdx", "acme"], "active_tenant": "acme"}
    payload = _synthesize_spa_access_token(attrs, sub="user-1")
    assert payload[CLAIM_SCOPE_NAME] == "acme"


def test_spa_token_payload_forbids_tenants_array():
    # D-5 enforcement: NO ``tenants[]`` or array-shaped tenant claims.
    attrs = {"memberships": ["gdx", "acme"], "active_tenant": "gdx"}
    payload = _synthesize_spa_access_token(attrs, sub="user-1")
    present_forbidden = D5_FORBIDDEN_CLAIMS & payload.keys()
    assert not present_forbidden, (
        f"payload contains forbidden D-5 claims: {sorted(present_forbidden)}"
    )


def test_spa_token_payload_aud_is_gdx_api():
    attrs = {"memberships": ["gdx"], "active_tenant": "gdx"}
    payload = _synthesize_spa_access_token(attrs, sub="user-1")
    assert payload["aud"] == "gdx-api"


def test_spa_token_payload_ttl_matches_15_minutes():
    fixed_now = 1_700_000_000
    attrs = {"memberships": ["gdx"], "active_tenant": "gdx"}
    payload = _synthesize_spa_access_token(attrs, sub="user-1", now=fixed_now)
    assert payload["iat"] == fixed_now
    assert payload["exp"] - payload["iat"] == SPA_ACCESS_TTL_SECONDS


def test_spa_token_payload_iss_points_at_gdx_spa_provider():
    attrs = {"memberships": ["gdx"], "active_tenant": "gdx"}
    payload = _synthesize_spa_access_token(attrs, sub="user-1")
    assert payload["iss"].endswith(f"/application/o/{GDX_SPA_PROVIDER_NAME}/")


def test_spa_token_payload_has_no_extra_non_contract_claims():
    # Slice A asserts only the D-5 required claims. No extra keys should
    # appear in the synthesized payload; future additions must be contract
    # changes (Architecture Reviewer path), not silent additions.
    attrs = {"memberships": ["gdx"], "active_tenant": "gdx"}
    payload = _synthesize_spa_access_token(attrs, sub="user-1")
    extra = payload.keys() - D5_REQUIRED_CLAIMS
    assert not extra, f"unexpected non-contract claims in payload: {sorted(extra)}"


# ---------------------------------------------------------------------------
# SS-6 Slice C — gdx-thirdparty provider payload contract
# ---------------------------------------------------------------------------


def test_supported_providers_includes_spa_and_thirdparty_but_not_mcp():
    # SS-6 supports two Authentik OAuth providers. gdx-mcp is PAT-only
    # (SS-14 / SS-19) and must never be an argparse choice.
    assert "gdx-spa" in SUPPORTED_PROVIDERS
    assert "gdx-thirdparty" in SUPPORTED_PROVIDERS
    assert "gdx-mcp" not in SUPPORTED_PROVIDERS


def test_thirdparty_provider_payload_enforces_pkce_s256_required():
    payload = build_gdx_thirdparty_provider_payload(
        signing_key_pk="sk", property_mapping_pks=["m"], authorization_flow_pk="f"
    )
    assert payload["pkce_mode"] == "required"


def test_thirdparty_provider_payload_forbids_implicit_flow():
    payload = build_gdx_thirdparty_provider_payload(
        signing_key_pk="sk", property_mapping_pks=["m"], authorization_flow_pk="f"
    )
    assert payload["response_types"] == ["code"]
    assert "token" not in payload["response_types"]
    assert "id_token" not in payload["response_types"]


def test_thirdparty_provider_payload_uses_fixed_redirect_uri():
    payload = build_gdx_thirdparty_provider_payload(
        signing_key_pk="sk", property_mapping_pks=["m"], authorization_flow_pk="f"
    )
    assert payload["redirect_uris"] == GDX_THIRDPARTY_REDIRECT_URI
    assert "*" not in payload["redirect_uris"]
    # Full URL must resolve to a non-wildcard GDX-owned host.
    assert payload["redirect_uris"].startswith("https://")


def test_thirdparty_provider_payload_audience_is_gdx_api():
    payload = build_gdx_thirdparty_provider_payload(
        signing_key_pk="sk", property_mapping_pks=["m"], authorization_flow_pk="f"
    )
    assert payload["audience"] == GDX_THIRDPARTY_AUDIENCE == "gdx-api"


def test_thirdparty_provider_payload_access_token_validity_is_1_hour():
    payload = build_gdx_thirdparty_provider_payload(
        signing_key_pk="sk", property_mapping_pks=["m"], authorization_flow_pk="f"
    )
    assert GDX_THIRDPARTY_ACCESS_TOKEN_VALIDITY == "hours=1"
    assert payload["access_token_validity"] == "hours=1"


def test_thirdparty_provider_payload_refresh_token_validity_is_90_days():
    payload = build_gdx_thirdparty_provider_payload(
        signing_key_pk="sk", property_mapping_pks=["m"], authorization_flow_pk="f"
    )
    assert GDX_THIRDPARTY_REFRESH_TOKEN_VALIDITY == "days=90"
    assert payload["refresh_token_validity"] == "days=90"


def test_thirdparty_provider_payload_client_id_distinct_from_spa():
    # Distinct client_ids so SPA and third-party clients rotate separately.
    spa = build_gdx_spa_provider_payload(
        signing_key_pk="spa-sk", property_mapping_pks=["m"], authorization_flow_pk="f"
    )
    tp = build_gdx_thirdparty_provider_payload(
        signing_key_pk="tp-sk", property_mapping_pks=["m"], authorization_flow_pk="f"
    )
    assert spa["client_id"] == "gdx-spa"
    assert tp["client_id"] == "gdx-thirdparty"
    assert spa["client_id"] != tp["client_id"]


def test_thirdparty_provider_payload_name_is_gdx_thirdparty():
    payload = build_gdx_thirdparty_provider_payload(
        signing_key_pk="sk", property_mapping_pks=["m"], authorization_flow_pk="f"
    )
    assert payload["name"] == GDX_THIRDPARTY_PROVIDER_NAME == "gdx-thirdparty"


def test_thirdparty_provider_payload_includes_property_mappings():
    payload = build_gdx_thirdparty_provider_payload(
        signing_key_pk="sk",
        property_mapping_pks=["gdx-tid-pk", "openid-pk", "profile-pk", "email-pk"],
        authorization_flow_pk="f",
    )
    assert payload["property_mappings"] == [
        "gdx-tid-pk",
        "openid-pk",
        "profile-pk",
        "email-pk",
    ]


def test_thirdparty_provider_payload_signing_key_and_redirect_differ_from_spa():
    # Defense-in-depth: separate signing key + separate redirect URI means
    # rotating/revoking the third-party key must not invalidate SPA tokens.
    spa = build_gdx_spa_provider_payload(
        signing_key_pk="spa-key", property_mapping_pks=["m"], authorization_flow_pk="f"
    )
    tp = build_gdx_thirdparty_provider_payload(
        signing_key_pk="tp-key", property_mapping_pks=["m"], authorization_flow_pk="f"
    )
    assert spa["signing_key"] != tp["signing_key"]
    assert spa["redirect_uris"] != tp["redirect_uris"]


# ---------------------------------------------------------------------------
# Synthesized gdx-thirdparty access token shape
# ---------------------------------------------------------------------------


def test_thirdparty_token_payload_has_all_d5_required_claims():
    attrs = {"memberships": ["gdx"], "active_tenant": "gdx"}
    payload = _synthesize_thirdparty_access_token(attrs, sub=str(uuid.uuid4()))
    missing = D5_REQUIRED_CLAIMS - payload.keys()
    assert not missing, f"payload missing required D-5 claims: {sorted(missing)}"


def test_thirdparty_token_payload_carries_singular_gdx_tid():
    attrs = {"memberships": ["gdx", "acme"], "active_tenant": "acme"}
    payload = _synthesize_thirdparty_access_token(attrs, sub="user-1")
    assert payload[CLAIM_SCOPE_NAME] == "acme"


def test_thirdparty_token_payload_forbids_tenants_array():
    attrs = {"memberships": ["gdx", "acme"], "active_tenant": "gdx"}
    payload = _synthesize_thirdparty_access_token(attrs, sub="user-1")
    present_forbidden = D5_FORBIDDEN_CLAIMS & payload.keys()
    assert not present_forbidden, (
        f"payload contains forbidden D-5 claims: {sorted(present_forbidden)}"
    )


def test_thirdparty_token_payload_aud_is_gdx_api():
    attrs = {"memberships": ["gdx"], "active_tenant": "gdx"}
    payload = _synthesize_thirdparty_access_token(attrs, sub="user-1")
    assert payload["aud"] == "gdx-api"


def test_thirdparty_token_payload_ttl_matches_1_hour():
    fixed_now = 1_700_000_000
    attrs = {"memberships": ["gdx"], "active_tenant": "gdx"}
    payload = _synthesize_thirdparty_access_token(attrs, sub="user-1", now=fixed_now)
    assert payload["iat"] == fixed_now
    assert payload["exp"] - payload["iat"] == THIRDPARTY_ACCESS_TTL_SECONDS
    # 1 hour exactly, not something close to it.
    assert THIRDPARTY_ACCESS_TTL_SECONDS == 3600


def test_thirdparty_token_payload_iss_points_at_gdx_thirdparty_provider():
    attrs = {"memberships": ["gdx"], "active_tenant": "gdx"}
    payload = _synthesize_thirdparty_access_token(attrs, sub="user-1")
    assert payload["iss"].endswith(f"/application/o/{GDX_THIRDPARTY_PROVIDER_NAME}/")
    # Isolation guard: must NOT be the SPA issuer URL.
    assert f"/application/o/{GDX_SPA_PROVIDER_NAME}/" not in payload["iss"]


def test_thirdparty_token_payload_has_no_extra_non_contract_claims():
    attrs = {"memberships": ["gdx"], "active_tenant": "gdx"}
    payload = _synthesize_thirdparty_access_token(attrs, sub="user-1")
    extra = payload.keys() - D5_REQUIRED_CLAIMS
    assert not extra, f"unexpected non-contract claims in payload: {sorted(extra)}"


def test_thirdparty_refresh_token_ttl_is_90_days():
    # Refresh token validity is a provider-config property, not a claim in
    # the access token itself. Assert the config constant pins 90 days so
    # the Authentik payload the script POSTs matches the SS-6 contract.
    assert THIRDPARTY_REFRESH_TTL_DAYS == 90
    assert f"days={THIRDPARTY_REFRESH_TTL_DAYS}" == GDX_THIRDPARTY_REFRESH_TOKEN_VALIDITY
