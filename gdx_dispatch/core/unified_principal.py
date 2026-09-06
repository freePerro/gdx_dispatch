"""Sprint 0.9 slice 0.9-c — unified ``Principal`` type.

Defines ONE ``Principal`` type. The auth-flow-specific dataclasses Sprint
0.8 shipped are all gone: the SS-7 ``core.principal`` variant, the SS-14
PAT, SS-22 SCIM and SS-21 OAuth-bearer modules, and (2026-09-06) the SS-32
SPIFFE ``AgentPrincipal`` with the middleware and package behind it.

``auth_kind`` names the two producers that exist: ``"session"`` (the login
JWT, via :mod:`gdx_dispatch.core.auth_dispatcher`) and ``"oauth"`` (the MCP
bridge's bearer principal in :mod:`gdx_dispatch.core.mcp_fastmcp_bridge`).
``actor_type`` likewise: ``"human"`` and ``"ai_worker"``.

Current wiring: slice 0.9-d shipped the composite dispatcher
``get_current_principal`` in :mod:`gdx_dispatch.core.auth_dispatcher`, which
produces this type for the session / login-JWT flow. Consumers today are :mod:`gdx_dispatch.routers.ai`,
:mod:`gdx_dispatch.core.mcp_protocol_adapter` and
:mod:`gdx_dispatch.core.mcp_fastmcp_bridge`. The 0.9-e sweep of the
remaining SS routers has NOT happened — most routers still take the SS-7
``Principal`` or a plain user dict.

Module-path deviation
---------------------
Task brief called for this file at ``gdx_dispatch/core/principal.py`` but that path
is already occupied by the SS-7 ``Principal`` dataclass (one of the five
variants that MUST stay in place for this slice per the task's own
scope note — "The 5 existing Principal variants STAY IN PLACE"). Colliding
on that module name would either silently overwrite SS-7's Principal
(breaking ~10 importers, several of which have since been deleted along with
the Authentik surface) or force a simultaneous rename of
SS-7, which is explicitly 0.9-e scope.

Parking the unified type at ``gdx_dispatch.core.unified_principal`` preserves the
"coexistence" contract the task specifies. Slice 0.9-e is expected to
collapse the path back to a single principal module when it deletes the five
variants.

Capability semantics
--------------------
``has_capability`` mirrors the canonical ``check_capability`` in
``gdx_dispatch.core.mcp_registry`` (SS-18), adapted for the tuple-shaped
capabilities this type carries:

* Exact match: ``(action, resource_type)`` in caps → allow.
* Action-scoped resource wildcard: ``(action, "*")`` → allow any resource
  for that action.
* Resource-scoped action wildcard: ``("*", resource_type)`` → allow any
  action on that resource.
* Superuser wildcard: ``("*", "*")`` → allow everything.
* Empty caps → deny (matches the ``if not caps`` short-circuit at the top
  of ``mcp_registry.check_capability``).
* Restricted flag: when ``is_restricted=True`` the principal is a
  capability-restricted bearer (v3 patch P36) and wildcards are disabled —
  only exact-match caps are honoured. This is the "restricted PAT" shape
  that narrows a wildcard-capable identity down to a specific allowlist.

``("*", "*")`` in the capability set is auto-detected in
``Principal.has_capability`` AND also flagged on the instance via
``is_super_admin=True`` (factory helpers set the flag at construction
time).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

__all__ = ["AuthKind", "Principal", "ActorType"]

AuthKind = Literal["session", "oauth"]

ActorType = Literal["human", "ai_worker"]



def _coerce_caps(
    caps: Iterable[Any],
) -> tuple[tuple[str, str], ...]:
    """Validate + coerce a capabilities iterable to the canonical shape.

    Accepts an iterable of 2-tuples/lists. Rejects dicts, mixed shapes,
    non-string entries, and empty strings. Always returns a ``tuple`` so
    the frozen dataclass stays hashable. A ``TypeError`` is raised on any
    shape mismatch — capabilities are security-critical; silent coercion
    would mask misuse.
    """
    if caps is None:
        raise TypeError("capabilities must be an iterable, not None")
    out: list[tuple[str, str]] = []
    for idx, item in enumerate(caps):
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise TypeError(
                "capabilities[%d] must be a 2-tuple of (action, resource_type), "
                "got %r" % (idx, item)
            )
        action, resource_type = item
        if not isinstance(action, str) or not action:
            raise TypeError(
                "capabilities[%d] action must be a non-empty str, got %r"
                % (idx, action)
            )
        if not isinstance(resource_type, str) or not resource_type:
            raise TypeError(
                "capabilities[%d] resource_type must be a non-empty str, got %r"
                % (idx, resource_type)
            )
        out.append((action, resource_type))
    return tuple(out)


def principal_tenant_uuid(principal: Principal) -> UUID | None:
    """D97 helper: cast Principal.tenant_id (UUID-stringified) to UUID for
    SQL comparisons against ``Membership.tenant_id`` (now ``Uuid``).

    Returns ``None`` for unresolved principals (e.g. Path 5 OAuth fallback
    ``oauth-unresolved:<client_id>``, Path 6 ``session-unresolved:<token>``);
    callers should treat None as "no tenant scope" — passing it through to
    a ``WHERE`` clause yields zero rows, which is the right safety default.
    """
    raw = getattr(principal, "tenant_id", None)
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


@dataclass(frozen=True)
class Principal:
    """Unified auth principal across the session and MCP-bearer flows.

    Produced by the composite ``get_current_principal`` dependency
    (slice 0.9-d). Consumed today by ``routers.ai`` and the MCP
    adapter/bridge — the 0.9-e sweep of the remaining SS routers is
    still outstanding, so this is not yet the universal router type.

    Fields
    ------
    identity_id
        UUID row id of the user (or a synthesized UUID5 for a non-human
        caller — see the MCP bridge).
    tenant_id
        Tenant UUID stringification (D97, 031). String form is kept on
        Principal so the Principal stays JSON-serializable for redis
        denylist + cross-process passes; for SQL comparisons against
        the now-UUID ``Membership.tenant_id`` column, callers cast via
        ``principal_tenant_uuid(principal)``. Always populated — there
        is no "tenantless" principal post-SS-22.
    principal_role
        Coarse role string. Canonical set in the codebase:
        "owner" | "admin" | "tech" | "viewer" | "agent". Tool/policy layers combine
        this with ``capabilities`` — the role alone does not grant access.
    capabilities
        Immutable tuple of ``(action, resource_type)`` pairs. See
        v3 patch P38 for the shape rationale. Empty tuple means no
        capabilities — ``has_capability`` returns False for everything.
    auth_kind
        Which flow produced this principal. Routers can gate on this
        for flow-specific behaviour (e.g. MCP-bearer-only handling).
    session_id, pat_id, oauth_token_id
        Auth-kind-specific audit/trace handles. Exactly one set is
        populated per principal based on ``auth_kind``; the others stay
        ``None``. Carrying them all on one type avoids isinstance churn
        in downstream routers.
    is_restricted
        v3 patch P36 — capability restriction flag. When True, wildcard
        capabilities are disabled by ``has_capability`` (only exact
        matches grant access).
    is_super_admin
        True when ``("*", "*")`` is in ``capabilities``. Set by the
        factory helpers; also set post-hoc by ``__post_init__`` so
        direct constructor use stays consistent.
    actor_type
        Discriminator for the nature of the principal (human, ai_worker, etc).
    delegated_by_user_id
        If actor_type is "ai_worker", the UUID of the human who authorized it.
    """

    identity_id: UUID
    tenant_id: str
    principal_role: str
    capabilities: tuple[tuple[str, str], ...]
    auth_kind: AuthKind
    # Auth-kind-specific metadata (all optional)
    session_id: str | None = None
    pat_id: UUID | None = None
    oauth_token_id: UUID | None = None
    # Cross-cutting flags
    is_restricted: bool = False
    is_super_admin: bool = False
    actor_type: ActorType = "human"
    delegated_by_user_id: str | None = None

    def __post_init__(self) -> None:
        # Validate + coerce capabilities to the canonical tuple shape.
        # Frozen dataclass — use object.__setattr__ to write in __post_init__.
        coerced = _coerce_caps(self.capabilities)
        object.__setattr__(self, "capabilities", coerced)

        # Auto-detect super-admin from caps so direct instantiation
        # stays consistent with factory helpers. We only flip False → True;
        # never clobber an explicit True (defensive).
        if ("*", "*") in coerced and not self.is_super_admin:
            object.__setattr__(self, "is_super_admin", True)

    # ── Capability check (SS-18 parity, tuple-shaped) ────────────────

    def has_capability(self, action: str, resource_type: str) -> bool:
        """Return True iff this principal may perform ``action`` on ``resource_type``.

        Mirrors ``gdx_dispatch.core.mcp_registry.check_capability`` semantics:

        * Empty caps → False.
        * Exact ``(action, resource_type)`` match → True.
        * Wildcard ``(action, "*")`` → True for any resource of that action.
        * Wildcard ``("*", resource_type)`` → True for any action on that resource.
        * Super-wildcard ``("*", "*")`` → True for everything.
        * ``is_restricted=True`` disables wildcards — exact match only.

        This is the *bare* capability check — descriptor-level
        ``sensitivity_class=="restricted"`` gating stays in
        ``mcp_registry.check_capability`` because it belongs to the
        tool descriptor, not the principal.
        """
        if not self.capabilities:
            return False
        if not isinstance(action, str) or not action:
            raise ValueError("action must be a non-empty str")
        if not isinstance(resource_type, str) or not resource_type:
            raise ValueError("resource_type must be a non-empty str")

        caps = self.capabilities
        needle = (action, resource_type)

        if needle in caps:
            return True

        if self.is_restricted:
            # Restricted principals: exact match only; no wildcard expansion.
            return False

        if ("*", "*") in caps:
            return True
        if (action, "*") in caps:
            return True
        return ("*", resource_type) in caps

    # ── Factory helpers (one per auth kind) ──────────────────────────

    @classmethod
    def from_session(
        cls,
        *,
        identity_id: UUID,
        tenant_id: str,
        role: str,
        capabilities: Iterable[tuple[str, str]],
        session_id: str,
        is_restricted: bool = False,
    ) -> Principal:
        """Construct a session/JWT-flow principal (SS-7)."""
        caps_t = _coerce_caps(capabilities)
        return cls(
            identity_id=identity_id,
            tenant_id=tenant_id,
            principal_role=role,
            capabilities=caps_t,
            auth_kind="session",
            session_id=session_id,
            is_restricted=is_restricted,
            is_super_admin=("*", "*") in caps_t,
        )
