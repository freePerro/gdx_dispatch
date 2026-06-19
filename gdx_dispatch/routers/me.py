"""SS-13 Slice A — login picker backend endpoint.

Exposes ``GET /api/me/tenants`` for an authenticated identity, returning the
list of non-revoked memberships as ``[{"slug", "name", "role"}, ...]``. Called
by the Slice B server-rendered picker handler (not yet landed) to decide
between auto-redirect (single tenant), picker UI (many), or signup (zero).

No module gate — this runs pre-tenant-selection, so the usual
``require_module`` guard would have nothing to key off of. No audit event —
the endpoint is read-only and fires before a tenant context exists.
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import Tenant
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.platform import Membership
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/me", tags=["me"])


@router.get("/tenants")
def list_my_tenants(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    identity_raw = user.get("sub") or user.get("user_id")
    if not identity_raw:
        raise HTTPException(status_code=401, detail="No identity on token")
    try:
        identity_id = UUID(str(identity_raw))
    except (ValueError, TypeError):
        # Legacy HS256 tokens minted before SS-6 carry non-UUID subjects and
        # therefore cannot match any platform ``Membership.identity_id``.
        # Return an empty list rather than 500.
        log.info("me_tenants_non_uuid_subject: %r", identity_raw)
        return []

    rows = db.execute(
        select(Membership, Tenant)
        .outerjoin(Tenant, Tenant.id == Membership.tenant_id)
        .where(
            Membership.identity_id == identity_id,
            Membership.revoked_at.is_(None),
        )
    ).all()

    return [
        {
            "slug": t.slug if t is not None else None,
            "name": t.name if t is not None else str(m.tenant_id),
            "role": m.role,
        }
        for m, t in rows
    ]
