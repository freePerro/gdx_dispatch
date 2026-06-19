"""SS-27 slice B — sharing event emit helpers.

Thin wrappers around :func:`gdx_dispatch.core.events.emit_event` that build the
canonical payload shapes defined in
``gdx_dispatch/core/event_schemas/gdx.sharing.*.v1.json``.

Metering policy (per SS-27 + SS-24): cross-tenant sharing is a billable
relationship — events are emitted with ``tenant_id = sharer_tenant_id``
so the SS-24 aggregator books the usage against the sharer. The sharee's
tenant is carried in the payload for audit + Command Center traceability.

Idempotency: the emitter layer is NOT responsible for dedup — that is
the caller's job. :func:`gdx_dispatch.core.cross_tenant_sharing.create_share`
only returns ``was_existing=False`` on a genuine new row, and callers
SHOULD gate ``emit_share_created`` on that flag. The emitter itself is
a pure translator and will append a row every time it is invoked.

No commits here — caller owns the transaction (matches
:mod:`gdx_dispatch.core.events`).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from gdx_dispatch.core.events import emit_event

logger = logging.getLogger(__name__)


SHARING_CREATED = "gdx_dispatch.sharing.created.v1"
SHARING_ACCEPTED = "gdx_dispatch.sharing.accepted.v1"
SHARING_REVOKED = "gdx_dispatch.sharing.revoked.v1"
SHARING_USED = "gdx_dispatch.sharing.used.v1"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


def emit_share_created(db: Session, share: Any) -> Any:
    """Emit ``gdx.sharing.created.v1`` — metered against the sharer."""
    payload = {
        "share_id": str(share.id),
        "sharer_tenant_id": str(share.sharer_tenant_id) if share.sharer_tenant_id is not None else None,
        "sharee_tenant_id": str(share.sharee_tenant_id) if share.sharee_tenant_id is not None else None,
        "resource_type": share.resource_type,
        "resource_id": share.resource_id,
        "capabilities": list(share.capabilities or []),
        "expires_at": _iso(share.expires_at),
        "created_by_identity_id": share.created_by_identity_id,
        "shared_at": _iso(share.shared_at) or "",
    }
    logger.info("cross_tenant_sharing: emit %s id=%s", SHARING_CREATED, share.id)
    return emit_event(db, SHARING_CREATED, payload, tenant_id=share.sharer_tenant_id)


def emit_share_accepted(db: Session, share: Any, acceptance: Any) -> Any:
    """Emit ``gdx.sharing.accepted.v1`` — metered against the sharer."""
    payload = {
        "share_id": str(share.id),
        "sharer_tenant_id": str(share.sharer_tenant_id) if share.sharer_tenant_id is not None else None,
        "sharee_tenant_id": str(share.sharee_tenant_id) if share.sharee_tenant_id is not None else None,
        "accepted_by_identity_id": acceptance.accepted_by_identity_id,
        "accepted_at": _iso(acceptance.accepted_at) or "",
    }
    logger.info("cross_tenant_sharing: emit %s id=%s", SHARING_ACCEPTED, share.id)
    return emit_event(db, SHARING_ACCEPTED, payload, tenant_id=share.sharer_tenant_id)


def emit_share_revoked(db: Session, share: Any) -> Any:
    """Emit ``gdx.sharing.revoked.v1`` — metered against the sharer."""
    payload = {
        "share_id": str(share.id),
        "sharer_tenant_id": str(share.sharer_tenant_id) if share.sharer_tenant_id is not None else None,
        "sharee_tenant_id": str(share.sharee_tenant_id) if share.sharee_tenant_id is not None else None,
        "revoked_by_identity_id": share.revoked_by_identity_id or "",
        "revoked_at": _iso(share.revoked_at) or "",
    }
    logger.info("cross_tenant_sharing: emit %s id=%s", SHARING_REVOKED, share.id)
    return emit_event(db, SHARING_REVOKED, payload, tenant_id=share.sharer_tenant_id)


def emit_share_used(
    db: Session,
    share: Any,
    *,
    capability: str,
    used_at: datetime,
    principal_identity_id: Optional[str] = None,
) -> Any:
    """Emit ``gdx.sharing.used.v1`` — metered against the sharer.

    Emitted by the middleware on successful cross-tenant access traversal.
    """
    payload = {
        "share_id": str(share.id),
        "sharer_tenant_id": str(share.sharer_tenant_id) if share.sharer_tenant_id is not None else None,
        "sharee_tenant_id": str(share.sharee_tenant_id) if share.sharee_tenant_id is not None else None,
        "resource_type": share.resource_type,
        "resource_id": share.resource_id,
        "capability": capability,
        "used_at": _iso(used_at) or "",
        "principal_identity_id": principal_identity_id,
    }
    logger.info("cross_tenant_sharing: emit %s id=%s", SHARING_USED, share.id)
    return emit_event(db, SHARING_USED, payload, tenant_id=share.sharer_tenant_id)
