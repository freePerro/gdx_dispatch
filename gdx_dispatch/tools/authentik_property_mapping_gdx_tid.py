"""Authentik property mapping for the singular ``gdx_tid`` claim (SS-6, D-5).

Pure-Python, no third-party imports — the body of :data:`SANDBOX_EXPRESSION`
has to run inside Authentik's restricted expression sandbox (which forbids
``import``), and :func:`build_gdx_tid_claims` is the host-side mirror used by
``configure_authentik.py`` and the token-shape tests.

D-5 contract: emit a single ``gdx_tid`` string claim. The legacy multi-tenant
``tenants[]`` / ``tenants_array`` / ``tid_list`` array claims are forbidden.
D18 assumption: every Authentik-linked identity is ``"human"`` until an
``Identity.type`` lands in the schema (see :data:`ASSUMED_IDENTITY_TYPE`).
"""
from __future__ import annotations

from typing import Any

# Name of the OIDC scope / claim Authentik emits. Single source of truth.
CLAIM_SCOPE_NAME = "gdx_tid"

# D18: Slice A assumes all Authentik identities are human until Identity.type
# lands. Kept as a constant so SS-7 can read it when it starts enforcing.
ASSUMED_IDENTITY_TYPE = "human"


def build_gdx_tid_claims(attrs: dict[str, Any]) -> dict[str, str]:
    """Resolve the singular ``gdx_tid`` claim from a user's tenant memberships.

    Fail-closed: ``memberships`` must be a non-empty list. ``active_tenant``,
    when present, must be one of the memberships; when absent, the first
    membership is used. Returns exactly ``{"gdx_tid": <tenant>}`` — never the
    forbidden multi-tenant array shape.
    """
    memberships = attrs.get("memberships")
    if not isinstance(memberships, list) or not memberships:
        raise ValueError("memberships claim is missing, empty, or not a list")

    active_tenant = attrs.get("active_tenant")
    if active_tenant is None:
        active_tenant = memberships[0]
    elif active_tenant not in memberships:
        raise ValueError("active_tenant is not one of the user's memberships")

    return {CLAIM_SCOPE_NAME: active_tenant}


# Body Authentik wraps in ``def mapping(user, request, db_session): ...``.
# Mirrors build_gdx_tid_claims; must stay import-free and syntactically valid.
SANDBOX_EXPRESSION = '''\
memberships = request.user.attributes.get("memberships")
if not isinstance(memberships, list) or not memberships:
    raise ValueError("memberships claim is missing, empty, or not a list")
active_tenant = request.user.attributes.get("active_tenant")
if active_tenant is None:
    active_tenant = memberships[0]
elif active_tenant not in memberships:
    raise ValueError("active_tenant is not one of the user's memberships")
return {"gdx_tid": active_tenant}
'''
