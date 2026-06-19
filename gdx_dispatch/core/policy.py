"""SS-7 Slice B — bounded policy evaluation core.

Scope (bounded)
---------------
This module is the pure-Python decision surface that SS-7 downstream
slices (denylist, live JWKS resolver) and SS-9 (``get_current_user`` +
``require_capability`` wiring) build on top of. It takes a validated
:class:`gdx_dispatch.core.principal.Principal` plus an action and a resource
reference, and returns a typed :class:`Decision`.

This module does NOT:

* wire a FastAPI ``Depends(require_capability(...))`` factory (SS-9),
* consult a revocation denylist (SS-7 Slice C),
* evaluate capability rows from the database (later SS-7 slices once
  ``Principal`` carries a ``capabilities`` field),
* evaluate RequestContext-based conditions such as ``max_amount``,
  ``ip_range``, ``time_window`` (plan §policy.py `_evaluate_conditions`
  lands after the capability row surface is available).

What it DOES enforce, today
---------------------------
* **Tenant isolation is first-class** — a :class:`Principal` whose
  ``tenant_id`` does not match the resource's ``tenant_id`` is
  deterministically denied, independent of action. This is the
  load-bearing D-5 contract guard; by landing it in the evaluator
  itself, every later SS-7 / SS-9 caller inherits it without having
  to re-check tenant membership at the router layer.
* **Default-deny on unknown actions** — any action not in
  :data:`ALLOWED_ACTIONS` is rejected. The allowlist is deliberately
  tiny (``read``, ``write``, ``delete``, ``list``) so later slices
  broaden it explicitly rather than relying on an implicit allow.
* **Typed, frozen decisions** — :class:`Decision` and
  :class:`ResourceRef` are frozen dataclasses so downstream callers
  cannot mutate a result after the fact, and so the reason string is
  stable for audit log / 403 body emission in SS-9.

SS-11 Slice B — decision tracing
--------------------------------
:func:`evaluate` emits a single child OpenTelemetry span named
``policy.decision`` around the decision computation. The span carries
stable attributes (``policy.decision.reason``,
``policy.decision.capability_matched``, and
``policy.decision.principal_role`` when the principal exposes a
non-empty ``role``) so audit/telemetry pipelines can reconstruct
allow/deny outcomes without re-running policy. Tracing is strictly
fail-open — a non-recording span, a missing optional attribute, or an
exception during attribute emission must not change the evaluation
result.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace

from gdx_dispatch.core.principal import Principal

logger = logging.getLogger(__name__)

_TRACER = trace.get_tracer(__name__)

_SPAN_NAME = "policy.decision"
_ATTR_REASON = "policy.decision.reason"
_ATTR_CAPABILITY_MATCHED = "policy.decision.capability_matched"
_ATTR_PRINCIPAL_ROLE = "policy.decision.principal_role"


ALLOWED_ACTIONS: frozenset[str] = frozenset({"read", "write", "delete", "list"})
"""Actions SS-7 Slice B's evaluator accepts as candidates for allow.

Intentionally small. Later SS-7 slices that add capability-row
evaluation will either extend this set or replace the default-deny
guard with a capability lookup; in both cases the replacement is an
explicit, reviewable edit rather than an implicit widening."""


DENY_CROSS_TENANT = "cross_tenant_access_denied"
"""Principal.tenant_id does not match ResourceRef.tenant_id (D-5)."""

DENY_UNKNOWN_ACTION = "unknown_action_default_deny"
"""Requested action is not in :data:`ALLOWED_ACTIONS`."""

DENY_MISSING_PRINCIPAL_TENANT = "principal_missing_tenant_id"
"""Principal.tenant_id is empty / blank. Defence in depth — the JWT
validator in :mod:`gdx_dispatch.core.auth_jwt` already raises
``MissingTenantClaim`` on empty ``gdx_tid``, but the evaluator refuses
to allow a request built from such a Principal regardless so
downstream callers cannot re-introduce the bypass."""

DENY_MISSING_RESOURCE_TENANT = "resource_missing_tenant_id"
"""ResourceRef.tenant_id is empty / blank. Callers must always attach
a tenant id to the resource being checked; an empty resource tenant
id is treated as an untrusted input."""

ALLOW_SAME_TENANT_ALLOWED_ACTION = "same_tenant_allowed_action"
"""Principal and resource tenants match AND action is on the
allowlist. The only path that returns ``allowed=True`` today."""


@dataclass(frozen=True)
class ResourceRef:
    """The resource a :func:`evaluate` call is being made against.

    ``tenant_id`` is required on every reference — tenant isolation is
    only expressible if the resource carries its owning tenant id.
    Callers must resolve this from the row (``company_id`` /
    ``tenant_id`` column) before calling :func:`evaluate`; the
    evaluator never guesses.

    Field names ``resource_type`` and ``instance_id`` match the SS-7
    plan §policy.py signature so later slices can unpack the ref into
    positional arguments or keep it as a bundle without breaking the
    import surface.
    """

    tenant_id: str
    resource_type: str
    instance_id: str | None = None


@dataclass(frozen=True)
class Decision:
    """Result of :func:`evaluate`.

    ``reason`` is a stable machine-readable constant (one of the
    ``DENY_*`` / ``ALLOW_*`` constants above), suitable for audit log
    emission and for assertion in unit tests.
    """

    allowed: bool
    reason: str


def evaluate(
    principal: Principal,
    action: str,
    resource: ResourceRef,
    context: Mapping[str, Any] | None = None,
) -> Decision:
    """Return :class:`Decision` for (``principal``, ``action``, ``resource``).

    Ordering matters and is load-bearing:

    1. Principal tenant sanity — empty ``tenant_id`` is rejected before
       anything else so the cross-tenant check cannot be bypassed by a
       blank-vs-blank accidental match.
    2. Resource tenant sanity — a resource with no tenant id is not
       evaluable; refuse rather than allow.
    3. Tenant isolation — mismatched tenant ids deny regardless of
       action. This is the D-5 guard.
    4. Action allowlist — unknown actions default-deny.

    ``context`` is accepted for future parity with the SS-7 plan
    ``_evaluate_conditions`` surface; this slice does not consume it
    and deliberately does not branch on it.

    Tracing (SS-11 Slice B): the decision is wrapped in a child OTel
    span named ``policy.decision`` with stable attributes. Tracing is
    fail-open — if the span is non-recording or attribute emission
    raises, the original :class:`Decision` is still returned.
    """
    del context  # reserved for later slices; intentionally unused here

    with _TRACER.start_as_current_span(_SPAN_NAME) as span:
        decision = _decide(principal, action, resource)
        try:
            _tag_decision_span(span, decision, principal)
        except Exception:  # noqa: BLE001 — tracing must never mutate result
            logger.exception("policy_decision_span_tag_failure")
        return decision


def _decide(
    principal: Principal,
    action: str,
    resource: ResourceRef,
) -> Decision:
    """Pure decision computation. Unchanged semantics from SS-7 Slice B.

    Kept as a module-private helper so :func:`evaluate` stays a thin
    tracing wrapper and every existing SS-7 Slice B invariant is
    enforced here in one place.
    """
    if not principal.tenant_id:
        return Decision(allowed=False, reason=DENY_MISSING_PRINCIPAL_TENANT)

    if not resource.tenant_id:
        return Decision(allowed=False, reason=DENY_MISSING_RESOURCE_TENANT)

    if principal.tenant_id != resource.tenant_id:
        return Decision(allowed=False, reason=DENY_CROSS_TENANT)

    if action not in ALLOWED_ACTIONS:
        return Decision(allowed=False, reason=DENY_UNKNOWN_ACTION)

    return Decision(allowed=True, reason=ALLOW_SAME_TENANT_ALLOWED_ACTION)


def _tag_decision_span(
    span: trace.Span,
    decision: Decision,
    principal: Principal,
) -> None:
    """Attach policy-decision attributes to the active span (fail-open).

    Skips emission silently when the span is non-recording (e.g. no OTel
    provider is installed, or sampling dropped the trace). Principal
    role is optional — only emitted when the principal exposes a
    non-empty ``role`` attribute, so current :class:`Principal` shapes
    that do not carry a role keep producing a clean two-attribute span.
    """
    if span is None or not span.is_recording():
        return

    span.set_attribute(_ATTR_REASON, decision.reason)
    span.set_attribute(_ATTR_CAPABILITY_MATCHED, bool(decision.allowed))

    role = getattr(principal, "role", None)
    if role is None:
        return
    role_text = str(role)
    if not role_text:
        return
    span.set_attribute(_ATTR_PRINCIPAL_ROLE, role_text)
