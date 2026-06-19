from typing import Any

class SelfScopeViolation(PermissionError):
    """Raised when a principal attempts to access a resource they do not own."""
    def __init__(self, principal_id: str, resource_type: str, resource_id: str):
        self.principal_id = principal_id
        self.resource_type = resource_type
        self.resource_id = resource_id
        super().__init__(
            f"Self-scope violation: Principal '{principal_id}' cannot access "
            f"{resource_type} '{resource_id}'"
        )

def assert_self_scope(
    principal: Any,
    resource_id: str,
    resource_type: str,
    *,
    owner_id: str | None = None
) -> None:
    """
    Validates that a principal has permission to access a resource based on ownership constraints.

    If the principal has a capability ending in '.own' for the given resource type,
    this function ensures the resource's owner_id matches the principal's identity_id.
    """
    if owner_id is None:
        raise ValueError("owner_id required for self-scope check")

    # Super-admin check
    if ("*", "*") in principal.capabilities:
        return

    # Check for ownership constraints
    # We check both read and write .own capabilities as per requirements.
    # The caller is responsible for the specific action, but if ANY .own cap exists,
    # the ownership constraint must be satisfied.
    has_own_constraint = False
    for _action, cap in principal.capabilities:
        if cap == f"{resource_type}.own":
            has_own_constraint = True
            break

    if has_own_constraint and str(owner_id) != str(principal.identity_id):
        raise SelfScopeViolation(
            principal_id=str(principal.identity_id),
            resource_type=resource_type,
            resource_id=resource_id
        )
