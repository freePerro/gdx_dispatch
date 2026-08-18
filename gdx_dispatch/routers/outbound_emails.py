"""Outbound email log — the queryable face of the audit trail.

Locked requirement (2026-08-18): for any email a customer did or didn't
receive, the office answers WHO/WHAT triggered it, WHAT it said, WHO it went
to, and WHAT happened — from the app, without container logs. The
outbound_emails table (written inside send_transactional_email for every
attempt) holds the answers; this router lists and shows them.

List responses omit body_html (rows can be large); the detail endpoint
returns it for the "what exactly did we send" question — render it in a
sandboxed iframe, same as the composer preview.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_role
from gdx_dispatch.models.tenant_models import OutboundEmail

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/outbound-emails",
    tags=["outbound-emails"],
    # Rendered bodies contain customer PII and money figures — office roles only.
    dependencies=[Depends(require_role("admin", "owner", "superadmin"))],
)

_MAX_LIMIT = 200


def _summary(row: OutboundEmail) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "status": row.status,
        "skip_reason": row.skip_reason,
        "provider": row.provider,
        "kind": row.kind,
        "initiator_kind": row.initiator_kind,
        "initiator_ref": row.initiator_ref,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "to_email": row.to_email,
        "to_name": row.to_name,
        "recipient_source": row.recipient_source,
        "subject": row.subject,
        "attachments": row.attachments_meta or [],
        "bounced_at": row.bounced_at.isoformat() if row.bounced_at else None,
    }


@router.get("", response_model=None)
def list_outbound_emails(
    status: str | None = Query(default=None, pattern="^(sent|failed)$"),
    kind: str | None = Query(default=None, max_length=20),
    initiator_kind: str | None = Query(default=None, max_length=20),
    entity_type: str | None = Query(default=None, max_length=30),
    entity_id: str | None = Query(default=None, max_length=64),
    to_email: str | None = Query(default=None, max_length=254),
    bounced: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    q = select(OutboundEmail)
    if status:
        q = q.where(OutboundEmail.status == status)
    if kind:
        q = q.where(OutboundEmail.kind == kind)
    if initiator_kind:
        q = q.where(OutboundEmail.initiator_kind == initiator_kind)
    if entity_type:
        q = q.where(OutboundEmail.entity_type == entity_type)
    if entity_id:
        q = q.where(OutboundEmail.entity_id == entity_id)
    if to_email:
        from sqlalchemy import func as _func

        q = q.where(_func.lower(OutboundEmail.to_email).contains(to_email.strip().lower()))
    if bounced is True:
        q = q.where(OutboundEmail.bounced_at.is_not(None))
    elif bounced is False:
        q = q.where(OutboundEmail.bounced_at.is_(None))
    rows = db.execute(
        q.order_by(OutboundEmail.created_at.desc()).offset(offset).limit(limit + 1)
    ).scalars().all()
    has_more = len(rows) > limit
    return {
        "items": [_summary(r) for r in rows[:limit]],
        "has_more": has_more,
        "offset": offset,
        "limit": limit,
    }


@router.get("/{outbound_id}", response_model=None)
def get_outbound_email(
    outbound_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.get(OutboundEmail, outbound_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Outbound email not found")
    out = _summary(row)
    out["body_html"] = row.body_html
    out["recipient_contact_id"] = row.recipient_contact_id
    return out
