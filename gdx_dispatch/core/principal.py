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


class ActorKind(str, enum.Enum):
    """Coarse actor classification derived from the issuing provider.

    Only the two OAuth providers are distinguished today.
    ``SERVICE_ACCOUNT`` is reserved for PAT bearers.
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
