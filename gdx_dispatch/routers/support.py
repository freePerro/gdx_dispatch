"""Tenant-side support submissions (cc2-s49a).

Two POST endpoints under ``/api/support`` — bug reports and feature
requests submitted from inside a tenant's GDX app, written directly
to the control-plane ``cc_support_tickets`` table for operator
visibility in the apartment-manager cockpit (cc2-s49b /api/cc/support
list/detail/assign/status/close).

Distinct from the legacy ``/api/feedback/bug-report`` flow in
``bug_reports.py`` — that one writes to the tenant-plane
``BugReport`` model. The s49a endpoints write to the control plane
so cross-tenant operator views (the cockpit support queue) work.
Both flows can coexist; tenant-plane keeps a record local to the
tenant DB, control-plane gives ops visibility.

Auth: standard tenant JWT via ``get_current_user``. Tenant ID comes
from ``request.state.tenant['id']`` (set by TenantMiddleware).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/support", tags=["support"])


class SupportSubmissionIn(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=5, max_length=5000)
    priority: str = Field(default="medium", pattern="^(low|medium|high|urgent)$")


class SupportSubmissionResponse(BaseModel):
    ticket_id: str
    status: str
    note: str


class MyTicketRow(BaseModel):
    id: str
    subject: str
    category: str
    status: str
    priority: str
    created_at: str
    closed_at: str | None
    resolution_summary: str | None


class MyTicketsResponse(BaseModel):
    items: list[MyTicketRow]


def _resolve_tenant_id(request: Request) -> str:
    """Extract tenant UUID from ``request.state.tenant`` (set by middleware)."""
    tenant = getattr(request.state, "tenant", None) or {}
    tid = tenant.get("id")
    if not tid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tenant context not resolved",
        )
    return str(tid)


def _resolve_user(user: dict[str, Any]) -> tuple[str, str | None]:
    """Return (email, user_id_uuid_str_or_none) from the JWT claims dict."""
    email = user.get("email") or user.get("preferred_username") or "anonymous@unknown"
    uid = user.get("sub") or user.get("user_id")
    return email, str(uid) if uid else None


def _create_ticket(
    db: Session,
    *,
    tenant_id: str,
    category: str,
    payload: SupportSubmissionIn,
    opened_by_email: str,
    opened_by_user_id: str | None,
) -> str:
    """INSERT into cc_support_tickets and return the new ticket id."""
    new_row = db.execute(
        sa_text(
            "INSERT INTO cc_support_tickets "
            "(tenant_id, opened_by_email, opened_by_user_id, "
            " subject, body, category, priority) "
            "VALUES (:tid, :email, :uid, :subject, :body, :cat, :prio) "
            "RETURNING id"
        ),
        {
            "tid": tenant_id,
            "email": opened_by_email,
            "uid": opened_by_user_id,
            "subject": payload.subject,
            "body": payload.body,
            "cat": category,
            "prio": payload.priority,
        },
    ).first()
    db.commit()
    return str(new_row.id)


@router.post(
    "/bug",
    response_model=SupportSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_bug(
    payload: SupportSubmissionIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SupportSubmissionResponse:
    """Submit a bug report. Lands in cc_support_tickets with category='bug'."""
    tenant_id = _resolve_tenant_id(request)
    email, uid = _resolve_user(user)
    ticket_id = _create_ticket(
        db,
        tenant_id=tenant_id,
        category="bug",
        payload=payload,
        opened_by_email=email,
        opened_by_user_id=uid,
    )
    log.info(
        "support bug — tenant=%s actor=%s ticket=%s",
        tenant_id, email, ticket_id,
    )
    return SupportSubmissionResponse(
        ticket_id=ticket_id,
        status="open",
        note="bug report received; the team will follow up via email.",
    )


@router.post(
    "/feature",
    response_model=SupportSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_feature(
    payload: SupportSubmissionIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SupportSubmissionResponse:
    """Submit a feature request. Lands in cc_support_tickets with category='feature'."""
    tenant_id = _resolve_tenant_id(request)
    email, uid = _resolve_user(user)
    ticket_id = _create_ticket(
        db,
        tenant_id=tenant_id,
        category="feature",
        payload=payload,
        opened_by_email=email,
        opened_by_user_id=uid,
    )
    log.info(
        "support feature — tenant=%s actor=%s ticket=%s",
        tenant_id, email, ticket_id,
    )
    return SupportSubmissionResponse(
        ticket_id=ticket_id,
        status="open",
        note="feature request received; thanks for the input.",
    )


@router.get("/my", response_model=MyTicketsResponse)
def list_my_tickets(
    request: Request,
    category: str | None = Query(None, pattern="^(bug|feature|question|other)$"),
    limit: int = Query(50, ge=1, le=200),
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MyTicketsResponse:
    """List the current tenant's support tickets (optionally filtered by category)."""
    tenant_id = _resolve_tenant_id(request)

    where = ["tenant_id = :tid"]
    params: dict[str, Any] = {"tid": tenant_id, "limit": limit}
    if category:
        where.append("category = :cat")
        params["cat"] = category

    sql = (
        f"SELECT id, subject, category, status, priority, "  # noqa: S608
        f"       created_at, closed_at, resolution_summary "
        f"FROM cc_support_tickets "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY created_at DESC "
        f"LIMIT :limit"
    )
    rows = db.execute(sa_text(sql), params).all()

    items = [
        MyTicketRow(
            id=str(r.id),
            subject=r.subject,
            category=r.category,
            status=r.status,
            priority=r.priority,
            created_at=r.created_at.isoformat(),
            closed_at=r.closed_at.isoformat() if r.closed_at else None,
            resolution_summary=r.resolution_summary,
        )
        for r in rows
    ]
    log.info(
        "support my-list — tenant=%s actor=%s n=%d",
        tenant_id, user.get("sub", "?"), len(items),
    )
    return MyTicketsResponse(items=items)
