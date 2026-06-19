"""SS-7 Slice A — Principal type for validated Authentik access tokens.

Scope (SS-7 Slice A, bounded): a pure-Python, ORM-free type that captures
the fields ``auth_jwt.validate_access_token`` extracts from a successfully
validated Authentik access token for the two SS-6 landed OAuth providers
(``gdx-spa``, ``gdx-thirdparty``).

This is intentionally minimal — downstream slices extend usage:

* SS-7 Slice B (policy engine) consumes ``Principal`` as the ``subject``
  input of ``evaluate(principal, action, resource, context)``.
* SS-9 wires ``request.state.principal`` from this type via the
  dual-protocol ``get_current_user``.
* SS-8 adds ``installation_id`` + ``act_chain`` fields for the asApp /
  signed-installation-token flow.

D-5 contract (SS-6 landed)
--------------------------
Tokens carry a SINGULAR ``gdx_tid`` claim. ``Principal.tenant_id`` is that
value, verbatim. There is NO ``tenants[]`` array; SS-7 validators reject
any token that attempts to add one.

D18 assumption (SS-6 Slice A)
-----------------------------
Authentik's scope mapping does not yet emit an ``identity_type`` claim —
``authentik_property_mapping_gdx_tid.ASSUMED_IDENTITY_TYPE = "human"`` is
the single source of truth. ``Principal.identity_type`` carries that
value on SPA tokens; third-party tokens surface ``ActorKind.THIRD_PARTY``
via ``actor_kind`` so downstream policy can distinguish them without
waiting for the ``Identity.type`` column to land.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from gdx_dispatch.core.contexts import current_act_chain, current_installation_id


class ActorKind(str, enum.Enum):
    """Coarse actor classification derived from the issuing provider.

    SS-7 Slice A only needs to distinguish the two SS-6 OAuth providers;
    ``SERVICE_ACCOUNT`` is reserved for SS-14 PAT bearers (MCP / signed
    installation tokens) and is defined here so later slices do not
    re-home the enum.
    """

    HUMAN = "human"
    THIRD_PARTY = "third_party"
    SERVICE_ACCOUNT = "service_account"


@dataclass(frozen=True)
class Principal:
    """Validated identity extracted from an Authentik access token.

    Constructed exclusively by ``gdx_dispatch.core.auth_jwt.validate_access_token``;
    never built directly from unverified token payloads.
    """

    tenant_id: str
    subject: str
    provider: str
    actor_kind: ActorKind
    identity_type: str
    issued_at: int
    expires_at: int
    issuer: str
    audience: str
    jti: str | None = None
    raw_claims: dict[str, Any] = field(default_factory=dict)
    installation_id: str | None = None
    act_chain: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionContext:
    """Typed snapshot of the SS-8 execution-context contextvars.

    Mirrors the subset of ``Principal`` fields that are populated from
    ContextVar state (``installation_id`` + ``act_chain``) rather than
    JWT claims. Frozen so the snapshot matches the immutability contract
    of ``Principal.act_chain`` and stays safe to share across audit
    middleware / policy-input builders.
    """

    installation_id: str | None
    act_chain: tuple[str, ...]


def current_execution_context() -> ExecutionContext:
    """Return a typed snapshot of the active SS-8 execution context.

    Reads :data:`gdx_dispatch.core.contexts.current_installation_id` and
    :data:`gdx_dispatch.core.contexts.current_act_chain` exactly once, in that
    order, and returns the result as a frozen
    :class:`ExecutionContext`. There are no side effects: the helper
    does not call ``.set(...)``, does not log, and does not raise — both
    contextvars carry stdlib-defined defaults so ``.get()`` is always
    well-defined.

    Default-context policy: a call made with no active
    ``execution_context(...)`` / ``async_execution_context(...)`` scope
    returns ``ExecutionContext(installation_id=None, act_chain=())``.
    That is a VALID result, not an error — it matches the
    ``Principal.installation_id`` / ``Principal.act_chain`` defaults
    seeded in SS-8 Slice A so plain-user requests produce the same
    Principal bytes whether or not they ever entered an asApp scope.
    Strict ("require delegation") variants belong at the policy /
    dependency layer, not here.
    """
    return ExecutionContext(
        installation_id=current_installation_id.get(),
        act_chain=current_act_chain.get(),
    )
