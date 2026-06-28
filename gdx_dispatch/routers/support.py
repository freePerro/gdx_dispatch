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
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import AppSettings
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


# cc_support_tickets lives in the CONTROL PLANE (provisioned by the Command
# Center's own alembic, migration 064_cc_support_tickets) — this app never
# creates it. On a DB where the CC migrations haven't run, the table is absent,
# so every support query errors. Detect *only that one case* and degrade
# gracefully instead of surfacing a raw 500 / "Database schema error".


def _is_missing_table(exc: Exception) -> bool:
    """True ONLY for a missing TABLE — never a missing column or other schema
    drift.

    A generic substring scan for "does not exist" is too broad: Postgres reports
    a missing column as ``column "x" does not exist`` (UndefinedColumn), and
    swallowing that would silently serve an empty list to a tenant who actually
    has tickets. So classify by the precise signal — SQLSTATE 42P01
    (UndefinedTable; 42703 is UndefinedColumn) on Postgres, or "no such table"
    on sqlite — not free text.
    """
    orig = getattr(exc, "orig", None)
    if getattr(orig, "pgcode", None) == "42P01":  # psycopg2 UndefinedTable
        return True
    return "no such table" in str(orig or exc).lower()  # sqlite


def _debug_logging_enabled(db: Session) -> bool:
    """Operator debug toggle (app_settings.debug_logging_enabled). Best-effort."""
    try:
        row = db.query(AppSettings).first()
        return bool(getattr(row, "debug_logging_enabled", False)) if row else False
    except Exception:
        return False


def _record_when_debug(db: Session, request: Request, exc: Exception) -> None:
    """When debug logging is on, surface an otherwise-swallowed error on the
    Server Errors page so operators can monitor cc_support_tickets health.

    Both the flag read and the sink write are best-effort — diagnostics must
    never turn a handled condition back into a failure.
    """
    if not _debug_logging_enabled(db):
        return
    try:
        from gdx_dispatch.modules.error_sink import record_server_error

        # 503, not 500: the support subsystem is unavailable, not crashed. The
        # GET still returns 200 (empty) to the client — recording 500 would
        # falsely imply an unhandled failure; 503 reads as "support down".
        record_server_error(
            request=request,
            exc=exc,
            status_code=503,
            request_id=getattr(getattr(request, "state", None), "request_id", None),
        )
    except Exception:  # pragma: no cover - diagnostics must not raise
        log.exception("support — failed to record debug error to server sink")


def _create_ticket(
    request: Request,
    db: Session,
    *,
    tenant_id: str,
    category: str,
    payload: SupportSubmissionIn,
    opened_by_email: str,
    opened_by_user_id: str | None,
) -> str:
    """INSERT into cc_support_tickets and return the new ticket id."""
    try:
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
    except (ProgrammingError, OperationalError) as exc:
        db.rollback()
        if _is_missing_table(exc):
            log.warning("support submit — cc_support_tickets unavailable (control plane not provisioned)")
            _record_when_debug(db, request, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Support ticketing is temporarily unavailable. Please try again later.",
            ) from exc
        raise
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
        request,
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
        request,
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
    try:
        rows = db.execute(sa_text(sql), params).all()
    except (ProgrammingError, OperationalError) as exc:
        db.rollback()
        if _is_missing_table(exc):
            # Control-plane table absent on this DB — show an empty list rather
            # than a 500 so the feedback page still renders.
            log.warning(
                "support my-list — cc_support_tickets unavailable; returning empty (tenant=%s)",
                tenant_id,
            )
            _record_when_debug(db, request, exc)
            return MyTicketsResponse(items=[])
        raise

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
