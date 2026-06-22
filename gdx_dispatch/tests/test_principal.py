"""Unit tests for the unified ``Principal`` type (Sprint 0.9 slice 0.9-c).

Tests cover:

* Immutability (frozen dataclass).
* Capabilities shape lock — reject non-tuple/list entries; coerce to tuple.
* Factory helpers populate the right ``auth_kind`` + audit handles.
* SPIFFE-id → identity_id UUID5 synthesis is deterministic.
* ``has_capability`` wildcard semantics match SS-18
  ``mcp_registry.check_capability`` (with restricted-flag override).

0.9-d will wire the composite ``get_current_principal`` dependency; 0.9-e
sweeps routers. This slice is type-only — no FastAPI / DB imports here.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4, uuid5

import pytest

from gdx_dispatch.core.unified_principal import (
    SPIFFE_ID_NAMESPACE,
    Principal,
)


def _make(**overrides):
    defaults = dict(
        identity_id=uuid4(),
        tenant_id="acme-corp",
        principal_role="admin",
        capabilities=(("read", "widget"),),
        auth_kind="session",
    )
    defaults.update(overrides)
    return Principal(**defaults)


# ── 1. Immutability ──────────────────────────────────────────────────


def test_principal_is_frozen():
    p = _make()
    with pytest.raises(FrozenInstanceError):
        p.principal_role = "owner"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        p.capabilities = (("x", "y"),)  # type: ignore[misc]


# ── 2. Capabilities shape lock ───────────────────────────────────────


def test_capabilities_coerces_list_of_lists_to_tuple_of_tuples():
    """Lists of 2-lists are accepted and coerced to tuple-of-tuples.

    Task-brief locked the shape via this test: we CHOSE coercion over
    rejection (security-critical-but-ergonomic — most real callers will
    hand us plain lists from JSON / DB rows).
    """
    p = _make(capabilities=[["read", "widget"], ["write", "gadget"]])
    assert p.capabilities == (("read", "widget"), ("write", "gadget"))
    assert isinstance(p.capabilities, tuple)
    for item in p.capabilities:
        assert isinstance(item, tuple)


def test_capabilities_rejects_malformed_entries():
    with pytest.raises(TypeError):
        _make(capabilities=[("read",)])  # wrong arity
    with pytest.raises(TypeError):
        _make(capabilities=[{"action": "read", "resource_type": "widget"}])  # dict form
    with pytest.raises(TypeError):
        _make(capabilities=[("", "widget")])  # empty action
    with pytest.raises(TypeError):
        _make(capabilities=[("read", 1)])  # non-string resource_type


# ── 3–7. Factory helpers ─────────────────────────────────────────────


def test_from_session_populates_auth_kind_session():
    iid = uuid4()
    tid = "acme-corp"
    p = Principal.from_session(
        identity_id=iid,
        tenant_id=tid,
        role="admin",
        capabilities=[("read", "widget")],
        session_id="sess-abc123",
    )
    assert p.auth_kind == "session"
    assert p.session_id == "sess-abc123"
    assert p.identity_id == iid
    assert p.tenant_id == tid
    assert p.principal_role == "admin"
    assert p.pat_id is None
    assert p.scim_token_id is None
    assert p.spiffe_id is None
    assert p.oauth_token_id is None


def test_from_spiffe_synthesizes_identity_from_uuid5():
    """Same spiffe_id → same identity_id across calls (deterministic)."""
    sid = "spiffe://garagedoor.ai/agent/dispatcher-01"
    tid = "acme-corp"
    p1 = Principal.from_spiffe(
        spiffe_id=sid, tenant_id=tid, capabilities=[("read", "*")]
    )
    p2 = Principal.from_spiffe(
        spiffe_id=sid, tenant_id="acme-corp", capabilities=[("write", "job")]
    )
    assert p1.identity_id == p2.identity_id
    assert p1.identity_id == uuid5(SPIFFE_ID_NAMESPACE, sid)
    assert p1.auth_kind == "spiffe"
    assert p1.spiffe_id == sid
    assert p1.principal_role == "agent"

    # A different spiffe_id produces a different identity_id.
    p3 = Principal.from_spiffe(
        spiffe_id="spiffe://garagedoor.ai/agent/other",
        tenant_id=tid,
        capabilities=[("read", "*")],
    )
    assert p3.identity_id != p1.identity_id


def test_from_spiffe_rejects_non_spiffe_id():
    with pytest.raises(ValueError):
        Principal.from_spiffe(
            spiffe_id="not-a-spiffe-id",
            tenant_id="acme-corp",
            capabilities=[("read", "*")],
        )


def test_from_oauth_carries_oauth_token_id():
    class FakeOauthToken:
        id = uuid4()

    tok = FakeOauthToken()
    p = Principal.from_oauth(
        oauth_token=tok,
        identity_id=uuid4(),
        tenant_id="acme-corp",
        role="oauth_client",
        capabilities=[("read", "customer")],
    )
    assert p.auth_kind == "oauth"
    assert p.oauth_token_id == tok.id
    assert p.principal_role == "oauth_client"


# ── 8–12. has_capability semantics ───────────────────────────────────


def test_has_capability_exact_match():
    p = _make(capabilities=[("read", "widget"), ("write", "gadget")])
    assert p.has_capability("read", "widget") is True
    assert p.has_capability("write", "gadget") is True
    assert p.has_capability("read", "gadget") is False
    assert p.has_capability("delete", "widget") is False


def test_has_capability_wildcard_star_star_grants_everything():
    p = _make(capabilities=[("*", "*")])
    assert p.is_super_admin is True
    assert p.has_capability("read", "widget") is True
    assert p.has_capability("delete", "tenant") is True
    assert p.has_capability("arbitrary", "thing") is True


def test_has_capability_resource_wildcard():
    """(action, "*") matches any resource for that action."""
    p = _make(capabilities=[("read", "*")])
    assert p.has_capability("read", "widget") is True
    assert p.has_capability("read", "gadget") is True
    assert p.has_capability("read", "anything") is True
    # But NOT other actions.
    assert p.has_capability("write", "widget") is False


def test_has_capability_action_wildcard():
    """("*", resource) matches any action on that resource — sibling of resource wildcard."""
    p = _make(capabilities=[("*", "widget")])
    assert p.has_capability("read", "widget") is True
    assert p.has_capability("write", "widget") is True
    # But NOT other resources.
    assert p.has_capability("read", "gadget") is False


def test_has_capability_restricted_requires_exact():
    """is_restricted=True disables wildcards — exact match only (v3 patch P36)."""
    p = _make(
        capabilities=[("read", "widget"), ("*", "*")],
        is_restricted=True,
    )
    # Exact match still works.
    assert p.has_capability("read", "widget") is True
    # Super-wildcard is disabled under restriction.
    assert p.has_capability("delete", "tenant") is False
    # Action wildcards similarly disabled.
    p2 = _make(capabilities=[("read", "*")], is_restricted=True)
    assert p2.has_capability("read", "widget") is False


def test_has_capability_empty_caps_denies():
    p = _make(capabilities=())
    assert p.has_capability("read", "widget") is False
    assert p.has_capability("*", "*") is False


def test_super_admin_auto_detected_on_direct_construction():
    """Constructing with ("*","*") sets is_super_admin even without explicit True."""
    p = _make(capabilities=[("*", "*")], auth_kind="session")
    assert p.is_super_admin is True


def test_factory_helpers_set_super_admin_flag():
    p = Principal.from_session(
        identity_id=uuid4(),
        tenant_id="acme-corp",
        role="owner",
        capabilities=[("*", "*")],
        session_id="s1",
    )
    assert p.is_super_admin is True
