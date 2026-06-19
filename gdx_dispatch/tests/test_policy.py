"""SS-7 Slice B — hermetic unit tests for :mod:`gdx_dispatch.core.policy`.

No FastAPI, no DB fixtures, no Redis, no JWKS. Every :class:`Principal`
is constructed in-process so the tests pin the bounded evaluator API
independently of the JWT validator (SS-7 Slice A) and the future
capability surface.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from gdx_dispatch.core.policy import (
    ALLOW_SAME_TENANT_ALLOWED_ACTION,
    ALLOWED_ACTIONS,
    DENY_CROSS_TENANT,
    DENY_MISSING_PRINCIPAL_TENANT,
    DENY_MISSING_RESOURCE_TENANT,
    DENY_UNKNOWN_ACTION,
    Decision,
    ResourceRef,
    evaluate,
)
from gdx_dispatch.core.principal import ActorKind, Principal

TENANT_ALPHA = "tenant-alpha-uuid"
TENANT_BRAVO = "tenant-bravo-uuid"


def _make_principal(
    *,
    tenant_id: str = TENANT_ALPHA,
    subject: str = "authentik-user-123",
    provider: str = "gdx-spa",
    actor_kind: ActorKind = ActorKind.HUMAN,
    identity_type: str = "human",
) -> Principal:
    """Construct a minimal :class:`Principal`.

    Kept local to the test module so the test suite does not depend on
    any JWT/Authentik fixture helper; SS-7 Slice A tests own that
    surface. This keeps the two suites independent under refactor.
    """
    return Principal(
        tenant_id=tenant_id,
        subject=subject,
        provider=provider,
        actor_kind=actor_kind,
        identity_type=identity_type,
        issued_at=1_700_000_000,
        expires_at=1_700_003_600,
        issuer="https://auth.example.com/application/o/gdx-spa/",
        audience="gdx-api",
    )


# ---------------------------------------------------------------------------
# Allow path
# ---------------------------------------------------------------------------


def test_allow_same_tenant_known_action():
    principal = _make_principal(tenant_id=TENANT_ALPHA)
    resource = ResourceRef(tenant_id=TENANT_ALPHA, resource_type="job")

    decision = evaluate(principal, "read", resource)

    assert decision.allowed is True
    assert decision.reason == ALLOW_SAME_TENANT_ALLOWED_ACTION


@pytest.mark.parametrize("action", sorted(ALLOWED_ACTIONS))
def test_every_allowed_action_allows_for_same_tenant(action):
    principal = _make_principal(tenant_id=TENANT_ALPHA)
    resource = ResourceRef(tenant_id=TENANT_ALPHA, resource_type="job")

    decision = evaluate(principal, action, resource)

    assert decision.allowed is True
    assert decision.reason == ALLOW_SAME_TENANT_ALLOWED_ACTION


def test_allow_ignores_instance_id_when_tenant_matches():
    principal = _make_principal(tenant_id=TENANT_ALPHA)
    resource = ResourceRef(
        tenant_id=TENANT_ALPHA,
        resource_type="job",
        instance_id="job-0001",
    )

    decision = evaluate(principal, "read", resource)

    assert decision.allowed is True


# ---------------------------------------------------------------------------
# Tenant isolation (D-5)
# ---------------------------------------------------------------------------


def test_deny_cross_tenant_even_for_known_action():
    principal = _make_principal(tenant_id=TENANT_ALPHA)
    resource = ResourceRef(tenant_id=TENANT_BRAVO, resource_type="job")

    decision = evaluate(principal, "read", resource)

    assert decision.allowed is False
    assert decision.reason == DENY_CROSS_TENANT


def test_deny_cross_tenant_for_third_party_actor():
    """ActorKind does not override the D-5 tenant isolation guard."""
    principal = _make_principal(
        tenant_id=TENANT_ALPHA,
        provider="gdx-thirdparty",
        actor_kind=ActorKind.THIRD_PARTY,
        identity_type="third_party",
    )
    resource = ResourceRef(tenant_id=TENANT_BRAVO, resource_type="job")

    decision = evaluate(principal, "read", resource)

    assert decision.allowed is False
    assert decision.reason == DENY_CROSS_TENANT


def test_deny_cross_tenant_takes_precedence_over_unknown_action():
    """If tenants mismatch AND action is unknown, the reason surfaces
    the tenant mismatch — not the unknown action — because the D-5
    guard runs first. Locking in this ordering makes audit log entries
    predictable and keeps attackers from probing the action allowlist
    by sending cross-tenant requests."""
    principal = _make_principal(tenant_id=TENANT_ALPHA)
    resource = ResourceRef(tenant_id=TENANT_BRAVO, resource_type="job")

    decision = evaluate(principal, "fabricate", resource)

    assert decision.allowed is False
    assert decision.reason == DENY_CROSS_TENANT


# ---------------------------------------------------------------------------
# Default-deny for unknown / unsupported actions
# ---------------------------------------------------------------------------


def test_deny_unknown_action_same_tenant():
    principal = _make_principal(tenant_id=TENANT_ALPHA)
    resource = ResourceRef(tenant_id=TENANT_ALPHA, resource_type="job")

    decision = evaluate(principal, "exfiltrate", resource)

    assert decision.allowed is False
    assert decision.reason == DENY_UNKNOWN_ACTION


def test_deny_empty_action_same_tenant():
    principal = _make_principal(tenant_id=TENANT_ALPHA)
    resource = ResourceRef(tenant_id=TENANT_ALPHA, resource_type="job")

    decision = evaluate(principal, "", resource)

    assert decision.allowed is False
    assert decision.reason == DENY_UNKNOWN_ACTION


def test_deny_case_sensitive_action_mismatch():
    """'READ' is not 'read' — the allowlist is case-sensitive to avoid
    silent drift from future capability-row data that may enforce a
    canonical lowercase form."""
    principal = _make_principal(tenant_id=TENANT_ALPHA)
    resource = ResourceRef(tenant_id=TENANT_ALPHA, resource_type="job")

    decision = evaluate(principal, "READ", resource)

    assert decision.allowed is False
    assert decision.reason == DENY_UNKNOWN_ACTION


# ---------------------------------------------------------------------------
# Defensive guards (blank tenant ids)
# ---------------------------------------------------------------------------


def test_deny_when_principal_tenant_id_is_empty():
    """Even if the resource tenant is also empty, an empty principal
    tenant must not accidentally 'match'. The principal check runs
    first so the blank-vs-blank case is closed deterministically."""
    principal = _make_principal(tenant_id="")
    resource = ResourceRef(tenant_id="", resource_type="job")

    decision = evaluate(principal, "read", resource)

    assert decision.allowed is False
    assert decision.reason == DENY_MISSING_PRINCIPAL_TENANT


def test_deny_when_resource_tenant_id_is_empty_but_principal_tenant_set():
    principal = _make_principal(tenant_id=TENANT_ALPHA)
    resource = ResourceRef(tenant_id="", resource_type="job")

    decision = evaluate(principal, "read", resource)

    assert decision.allowed is False
    assert decision.reason == DENY_MISSING_RESOURCE_TENANT


# ---------------------------------------------------------------------------
# Context passthrough is reserved but unused in this slice
# ---------------------------------------------------------------------------


def test_context_is_accepted_but_does_not_influence_result():
    """`context` is reserved for the future RequestContext surface and
    intentionally ignored in Slice B. This test pins that contract so
    a later slice that starts branching on `context` breaks loudly
    here rather than silently altering allow/deny semantics."""
    principal = _make_principal(tenant_id=TENANT_ALPHA)
    resource = ResourceRef(tenant_id=TENANT_ALPHA, resource_type="job")

    allowed = evaluate(principal, "read", resource, context=None)
    with_context = evaluate(
        principal,
        "read",
        resource,
        context={"request_ip": "10.0.0.1", "request_amount": 999_999},
    )

    assert allowed == with_context
    assert allowed.allowed is True


# ---------------------------------------------------------------------------
# Type invariants
# ---------------------------------------------------------------------------


def test_decision_is_frozen():
    decision = Decision(allowed=True, reason=ALLOW_SAME_TENANT_ALLOWED_ACTION)
    with pytest.raises(FrozenInstanceError):
        decision.allowed = False  # type: ignore[misc]


def test_resource_ref_is_frozen():
    resource = ResourceRef(tenant_id=TENANT_ALPHA, resource_type="job")
    with pytest.raises(FrozenInstanceError):
        resource.tenant_id = TENANT_BRAVO  # type: ignore[misc]


def test_allowed_actions_is_frozenset():
    """ALLOWED_ACTIONS must be immutable so downstream callers cannot
    mutate the allowlist at runtime to escalate privilege."""
    assert isinstance(ALLOWED_ACTIONS, frozenset)
    assert {"read", "write", "delete", "list"} == set(ALLOWED_ACTIONS)
