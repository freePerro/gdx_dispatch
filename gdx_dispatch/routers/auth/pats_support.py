"""Support endpoints for the PAT-management UI (SS-14/SS-15 completion).

The Vue views ``SettingsApiKeys.vue`` and ``TenantAdminApiKeys.vue`` were
shipped ahead of these two backend endpoints (TODO markers in
both files). This module fills the gap so the UI stops 404ing in
production:

- ``GET /api/capabilities/available`` — capabilities the caller may grant
  when minting a PAT. Enforces the subset-of-self rule from pats.py.
- ``GET /api/admin/tenant-members`` — identities that are members of the
  caller's tenant, used by TenantAdminApiKeys.vue's target-user picker
  for admin-on-behalf issuance (SS-15 admin_pats.py).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.auth_dispatcher import get_current_principal
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.unified_principal import Principal, principal_tenant_uuid
from gdx_dispatch.models.platform import Identity, Membership

router = APIRouter(tags=["pats-support"])


def _is_tenant_admin(principal: Principal) -> bool:
    """Match admin_pats.py's _is_tenant_admin — same gate for consistency."""
    caps = set(principal.capabilities)
    if ("admin", "tenant") in caps or ("*", "*") in caps:
        return True
    return getattr(principal, "principal_role", None) in ("owner", "admin")


@router.get("/api/capabilities/available")
def list_available_capabilities(
    principal: Principal = Depends(get_current_principal),
) -> list[dict[str, str]]:
    """Return the capabilities the caller may grant on a new PAT.

    Enforces the subset-of-self rule: a caller may only mint PATs whose
    capabilities are a subset of their own (pats.py D-13/D-14). The UI
    uses this list to render a checkbox grid of grantable capabilities.
    A ``("*", "*")`` wildcard is returned verbatim so the UI can present
    "grant any capability" when the caller holds it.
    """
    return [
        {"action": action, "resource_type": resource_type}
        for action, resource_type in principal.capabilities
    ]


@router.get("/api/admin/tenant-members")
def list_tenant_members(
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """List identities that are active members of the caller's tenant.

    Admin-gated (matches SS-15 admin_pats.py policy). Returns the shape
    TenantAdminApiKeys.vue expects for its target-user picker:
    ``{identity_id, email, display_name, role}``.
    """
    if not _is_tenant_admin(principal):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "tenant admin role required")

    rows = db.execute(
        select(Membership, Identity)
        .join(Identity, Identity.id == Membership.identity_id)
        .where(Membership.tenant_id == principal_tenant_uuid(principal))
        .where(Membership.revoked_at.is_(None))
        .where(Identity.deleted_at.is_(None))
        .order_by(Identity.email)
    ).all()

    return [
        {
            "identity_id": str(identity.id),
            "email": identity.email,
            "display_name": identity.display_name,
            "role": membership.role,
        }
        for membership, identity in rows
    ]
