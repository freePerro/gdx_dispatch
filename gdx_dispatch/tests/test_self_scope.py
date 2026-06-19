import pytest
from dataclasses import dataclass
from gdx_dispatch.core.self_scope import assert_self_scope, SelfScopeViolation

@dataclass
class MockPrincipal:
    identity_id: str
    capabilities: set[tuple[str, str]]

def test_self_scope_matching_owner():
    principal = MockPrincipal(identity_id="user_123", capabilities={("read", "customer.own")})
    # Should not raise
    assert_self_scope(principal, "cust_abc", "customer", owner_id="user_123")

def test_self_scope_non_matching_owner():
    principal = MockPrincipal(identity_id="user_123", capabilities={("read", "customer.own")})
    # Should raise SelfScopeViolation
    with pytest.raises(SelfScopeViolation) as excinfo:
        assert_self_scope(principal, "cust_abc", "customer", owner_id="user_456")
    assert excinfo.value.principal_id == "user_123"
    assert excinfo.value.resource_type == "customer"
    assert excinfo.value.resource_id == "cust_abc"

def test_self_scope_broad_capability_wins():
    # If they have the broad 'customer' cap, the '.own' constraint is bypassed
    principal = MockPrincipal(identity_id="user_123", capabilities={("read", "customer")})
    # Should not raise even if owner doesn't match
    assert_self_scope(principal, "cust_abc", "customer", owner_id="user_456")

def test_self_scope_super_admin():
    principal = MockPrincipal(identity_id="admin", capabilities={("*", "*")})
    # Should not raise
    assert_self_scope(principal, "any_id", "any_type", owner_id="someone_else")

def test_self_scope_no_relevant_cap():
    # No capability for 'customer' at all
    principal = MockPrincipal(identity_id="user_123", capabilities={("read", "job")})
    # If the helper is called for 'customer' and no cap exists, it's a no-op for THIS helper
    # (The caller's actual permission check would fail elsewhere)
    assert_self_scope(principal, "cust_abc", "customer", owner_id="user_456")

def test_self_scope_missing_owner_id():
    principal = MockPrincipal(identity_id="user_123", capabilities={("read", "customer.own")})
    with pytest.raises(ValueError, match="owner_id required for self-scope check"):
        assert_self_scope(principal, "cust_abc", "customer", owner_id=None)
