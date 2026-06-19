"""Sprint MCP-Streamable-HTTP S4 — bearer mint + verify (in-process unit tests).

Cross-tenant denial at the transport layer is a separate test
(``test_mcp_cross_tenant_denial.py``) — that one stands the full
ASGI stack up. Here we exercise just the mint/verify primitives so
failures localize cleanly.
"""
from __future__ import annotations

import time
import uuid

import jwt
import pytest

from gdx_dispatch.core.mcp_bearer import (
    BearerInvalid,
    MCPClaims,
    mint_mcp_access_token,
    verify_mcp_bearer,
)


GDX_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")
ACME_UUID = uuid.UUID("22222222-2222-2222-2222-222222222222")
GDX_ISSUER = "https://gdx.example.com"
GDX_AUDIENCE = f"{GDX_ISSUER}/mcp"
ACME_ISSUER = "https://acme.example.com"
ACME_AUDIENCE = f"{ACME_ISSUER}/mcp"


def _mint(tenant_id=GDX_UUID, issuer=GDX_ISSUER, audience=GDX_AUDIENCE, ttl=3600):
    return mint_mcp_access_token(
        tenant_id=tenant_id,
        subject_id="user@example.com",
        issuer=issuer,
        audience=audience,
        scope="mcp:invoke",
        ttl_seconds=ttl,
    )


# ── happy path ──────────────────────────────────────────────────────────────


def test_mint_then_verify_returns_claims():
    token = _mint()
    claims = verify_mcp_bearer(
        token,
        expected_issuer=GDX_ISSUER,
        expected_audience=GDX_AUDIENCE,
        expected_tenant_id=GDX_UUID,
    )
    assert isinstance(claims, MCPClaims)
    assert claims.tenant_id == str(GDX_UUID)
    assert claims.audience == GDX_AUDIENCE
    assert claims.issuer == GDX_ISSUER
    assert claims.sub == "user@example.com"
    assert claims.scope == "mcp:invoke"
    assert claims.has_scope is True


def test_uuid_arg_and_string_arg_interchangeable():
    """Caller may pass UUID or str — verify must accept either."""
    token = _mint(tenant_id=GDX_UUID)
    claims = verify_mcp_bearer(
        token,
        expected_issuer=GDX_ISSUER,
        expected_audience=GDX_AUDIENCE,
        expected_tenant_id=str(GDX_UUID),  # string form
    )
    assert claims.tenant_id == str(GDX_UUID)


# ── cross-tenant denial — the load-bearing security gate ────────────────────


def test_token_for_one_tenant_rejected_against_another_tenants_audience():
    """Token minted for gdx must NOT verify against acme's audience."""
    token = _mint(tenant_id=GDX_UUID, issuer=GDX_ISSUER, audience=GDX_AUDIENCE)
    with pytest.raises(BearerInvalid):
        verify_mcp_bearer(
            token,
            expected_issuer=ACME_ISSUER,
            expected_audience=ACME_AUDIENCE,
            expected_tenant_id=ACME_UUID,
        )


def test_audience_mismatch_alone_is_rejected():
    """Even if the issuer matches, an aud claim for tenant A on tenant B's
    /mcp must be rejected."""
    token = _mint(audience=GDX_AUDIENCE)  # /mcp on gdx
    with pytest.raises(BearerInvalid):
        verify_mcp_bearer(
            token,
            expected_issuer=GDX_ISSUER,
            expected_audience=ACME_AUDIENCE,  # different /mcp
            expected_tenant_id=GDX_UUID,
        )


def test_gdx_tid_mismatch_is_rejected():
    """If somehow aud + iss match but gdx_tid does not — reject. This
    is the defense-in-depth check beyond aud."""
    token = _mint(tenant_id=GDX_UUID)
    with pytest.raises(BearerInvalid, match="gdx_tid mismatch"):
        verify_mcp_bearer(
            token,
            expected_issuer=GDX_ISSUER,
            expected_audience=GDX_AUDIENCE,
            expected_tenant_id=ACME_UUID,
        )


# ── temporal + structural failures ──────────────────────────────────────────


def test_expired_token_is_rejected():
    """Negative TTL → exp in the past → reject."""
    token = _mint(ttl=-60)
    with pytest.raises(BearerInvalid):
        verify_mcp_bearer(
            token,
            expected_issuer=GDX_ISSUER,
            expected_audience=GDX_AUDIENCE,
            expected_tenant_id=GDX_UUID,
        )


def test_missing_required_claim_is_rejected():
    """A token signed by us but lacking gdx_tid must be rejected."""
    import os

    secret = os.environ["JWT_SECRET"]
    bad_payload = {
        "iss": GDX_ISSUER,
        "aud": GDX_AUDIENCE,
        "sub": "user@example.com",
        "scope": "mcp:invoke",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        # no gdx_tid claim
    }
    bad_token = jwt.encode(bad_payload, secret, algorithm="HS256")
    with pytest.raises(BearerInvalid):
        verify_mcp_bearer(
            bad_token,
            expected_issuer=GDX_ISSUER,
            expected_audience=GDX_AUDIENCE,
            expected_tenant_id=GDX_UUID,
        )


def test_wrong_signature_is_rejected():
    """A JWT signed with the wrong key — even with all the right claims —
    must be rejected."""
    bad_payload = {
        "iss": GDX_ISSUER,
        "aud": GDX_AUDIENCE,
        "sub": "x",
        "gdx_tid": str(GDX_UUID),
        "scope": "mcp:invoke",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    bad_token = jwt.encode(bad_payload, "x" * 64, algorithm="HS256")  # wrong key
    with pytest.raises(BearerInvalid):
        verify_mcp_bearer(
            bad_token,
            expected_issuer=GDX_ISSUER,
            expected_audience=GDX_AUDIENCE,
            expected_tenant_id=GDX_UUID,
        )


def test_garbage_token_is_rejected():
    with pytest.raises(BearerInvalid):
        verify_mcp_bearer(
            "this.is.not.a.jwt",
            expected_issuer=GDX_ISSUER,
            expected_audience=GDX_AUDIENCE,
            expected_tenant_id=GDX_UUID,
        )
