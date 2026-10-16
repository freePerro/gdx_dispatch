"""What a composer may attach — ``GET /api/email-attachments/sources``.

Feeds the attachment picker in the inbox's new / reply / forward, on
desktop and phone:
the job's photos and files, the customer's documents, and every estimate and
invoice of that customer the caller may see — the list a sender chooses
several estimates from. Read-only.

Filtered by the same :class:`core.email_attachments.AttachScope` the send
path enforces, and only files still on disk are listed, so a send never
refuses a listed record as unreachable or missing. A listed photo can still
be refused when it cannot be cleaned of its location or is over 64MP
(``_clean_photo``). Any logged-in user may call it; what comes back is
narrowed to their reach (a technician sees their own jobs' records).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.email_attachments import list_attachment_sources

router = APIRouter(prefix="/api/email-attachments", tags=["email-attachments"])


def _tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", None) or {}
    tid = tenant.get("id") if isinstance(tenant, dict) else None
    if not tid:
        raise HTTPException(status_code=400, detail="missing tenant context")
    return str(tid)


@router.get("/sources")
def attachment_sources(
    request: Request,
    job_id: str | None = Query(default=None, max_length=64),
    customer_id: str | None = Query(default=None, max_length=64),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return list_attachment_sources(
        db, request, current_user, _tenant_id(request),
        job_id=job_id, customer_id=customer_id,
    )
