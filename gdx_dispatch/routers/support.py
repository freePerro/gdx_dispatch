"""Tenant-side support submissions (cc2-s49a; tenant-plane since 2026-07).

Two POST endpoints under ``/api/support`` — bug reports and feature
requests submitted from inside the GDX app — plus the ``/my`` list the
feedback portal reads.

Originally these wrote to the control-plane ``cc_support_tickets``
table (Command Center alembic 064) for the multi-tenant cockpit. A
single-tenant install never provisions that table, so every submission
503'd and ``/my`` served an empty list — prod lost two real reports
before the 2026-07-07 audit caught it. Tickets now land in the
tenant-plane ``SupportTicket`` model (``support_tickets``), created at
deploy by ``create_orm_tables()`` like every other ORM table.

The in-app bug button (``BugReportButton.vue``) posts here and nowhere
else since 2026-09-06.

Auth: standard tenant JWT via ``get_current_user``. Tenant ID comes
from ``request.state.tenant['id']`` (set by TenantMiddleware).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import audit_or_rollback, ensure_audit_table, log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import has_permission, require_permission
from gdx_dispatch.models.tenant_models import AppSettings, SupportTicket, User
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
    # #622: the description the reporter typed (with the page URL and browser
    # the bug button appends) was stored and shown nowhere. Only for the team
    # (settings.write — the key that can close a ticket) and for the ticket's
    # own reporter: the bug button files from any page, customer and invoice
    # pages included, so a body can carry customer details, and /my answers
    # every signed-in role. Everyone else gets the list as it always was.
    # The body is the protected part. Who filed a ticket is not a secret: the
    # ticket's audit rows name the reporter, and GET /api/activity/recent
    # shows audit rows to any signed-in user.
    body: str | None
    opened_by_email: str | None
    category: str
    status: str
    priority: str
    created_at: str
    closed_at: str | None
    resolution_summary: str | None


class CloseTicketIn(BaseModel):
    resolution_summary: str = Field(min_length=1, max_length=5000)


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


_PLACEHOLDER_EMAILS = {"anonymous@unknown", "unknown@bug-reports"}


def _user_emails(db: Session, user_ids) -> dict[str, str]:
    """``{user_id: email}`` for the given ids, in one query."""
    ids = set()
    for uid in user_ids:
        try:
            ids.add(UUID(str(uid)))
        except (TypeError, ValueError):
            continue
    if not ids:
        return {}
    rows = db.execute(select(User.id, User.email).where(User.id.in_(ids))).all()
    return {str(uid): email for uid, email in rows if email}


def _resolve_user(db: Session, user: dict[str, Any]) -> tuple[str, str | None]:
    """Return (email, user_id) for the submitter.

    #622: the JWT carries no ``email`` claim, so this used to fall back to
    ``anonymous@unknown`` — 5 of the 7 tickets on prod name nobody, though
    every one carries the user id. The user row knows the email.
    """
    uid = user.get("sub") or user.get("user_id")
    uid = str(uid) if uid else None
    email = user.get("email") or user.get("preferred_username")
    if not email and uid:
        email = _user_emails(db, [uid]).get(_normalize_id(uid))
    return email or "anonymous@unknown", uid


def _normalize_id(uid: Any) -> str:
    try:
        return str(UUID(str(uid)))
    except (TypeError, ValueError):
        return str(uid)


def _reporter_email(ticket: SupportTicket, emails: dict[str, str]) -> str | None:
    """The stored reporter, unless it is a placeholder a user id can replace —
    display-time, so the rows already filed as anonymous read correctly without
    rewriting them."""
    stored = ticket.opened_by_email
    if stored and stored not in _PLACEHOLDER_EMAILS:
        return stored
    if ticket.opened_by_user_id:
        return emails.get(_normalize_id(ticket.opened_by_user_id)) or stored
    return stored


# support_tickets is created by create_orm_tables() at container start, but a
# DB from an image that predates the model (or a container racing its own
# entrypoint) can still lack it. Detect *only that one case* and degrade
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
    Server Errors page so operators can monitor support_tickets health.

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
    """INSERT into support_tickets and return the new ticket id."""
    ticket = SupportTicket(
        id=str(uuid4()),
        tenant_id=tenant_id,
        opened_by_email=opened_by_email,
        opened_by_user_id=opened_by_user_id,
        subject=payload.subject,
        body=payload.body,
        category=category,
        priority=payload.priority,
        status="open",
        created_at=datetime.now(UTC),
    )
    try:
        # Before anything is staged: the first audit write on an engine
        # initializes the guard and would commit the ticket ahead of its row.
        ensure_audit_table(db)
        db.add(ticket)
        # Invariant #1: who filed it, what, when — staged into the SAME commit
        # as the ticket (#622/#700). It used to be written after the commit and
        # never committed at all: get_db() closes without committing, so the
        # row was rolled back on every submission — 0 rows on prod for 7
        # tickets.
        log_audit_event_sync(
            db, tenant_id=tenant_id, user_id=opened_by_user_id or "system", action="create",
            entity_type="support_ticket", entity_id=ticket.id,
            details={"category": category, "subject": payload.subject, "priority": payload.priority},
            request=request,
        )
        db.commit()
    except (ProgrammingError, OperationalError) as exc:
        db.rollback()
        if _is_missing_table(exc):
            log.warning("support submit — support_tickets table missing (pre-create_orm_tables DB?)")
            _record_when_debug(db, request, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Support ticketing is temporarily unavailable. Please try again later.",
            ) from exc
        raise
    return ticket.id


def _caller_id(user: dict[str, Any]) -> str | None:
    uid = user.get("sub") or user.get("user_id")
    return str(uid) if uid else None


def _row(r: SupportTicket, emails: dict[str, str], *, detail: bool) -> MyTicketRow:
    return MyTicketRow(
        id=str(r.id),
        subject=r.subject,
        body=(r.body or "") if detail else None,
        opened_by_email=_reporter_email(r, emails) if detail else None,
        category=r.category,
        status=r.status,
        priority=r.priority,
        created_at=r.created_at.isoformat(),
        closed_at=r.closed_at.isoformat() if r.closed_at else None,
        resolution_summary=r.resolution_summary,
    )


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
    """Submit a bug report. Lands in support_tickets with category='bug'."""
    tenant_id = _resolve_tenant_id(request)
    email, uid = _resolve_user(db, user)
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
    """Submit a feature request. Lands in support_tickets with category='feature'."""
    tenant_id = _resolve_tenant_id(request)
    email, uid = _resolve_user(db, user)
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

    query = db.query(SupportTicket)
    if category:
        query = query.filter(SupportTicket.category == category)
    try:
        rows = (
            query.order_by(SupportTicket.created_at.desc()).limit(limit).all()
        )
    except (ProgrammingError, OperationalError) as exc:
        db.rollback()
        if _is_missing_table(exc):
            # Table absent on this DB — show an empty list rather than a 500
            # so the feedback page still renders.
            log.warning(
                "support my-list — support_tickets table missing; returning empty (tenant=%s)",
                tenant_id,
            )
            _record_when_debug(db, request, exc)
            return MyTicketsResponse(items=[])
        raise

    team = has_permission(request, db, "settings.write")
    caller = _caller_id(user)
    caller_key = _normalize_id(caller) if caller else None

    def _may_read(r: SupportTicket) -> bool:
        return team or (
            caller_key is not None
            and r.opened_by_user_id is not None
            and _normalize_id(r.opened_by_user_id) == caller_key
        )

    readable = [r for r in rows if _may_read(r)]
    emails = _user_emails(db, [r.opened_by_user_id for r in readable])
    items = [_row(r, emails, detail=_may_read(r)) for r in rows]
    log.info(
        "support my-list — tenant=%s actor=%s n=%d",
        tenant_id, _caller_id(user) or "?", len(items),
    )
    return MyTicketsResponse(items=items)


@router.post(
    "/tickets/{ticket_id}/close",
    response_model=MyTicketRow,
    # Owner/admin: the Feedback Portal lives in the Admin nav, and
    # settings.write is the key the admin pages share. A reporter cannot close
    # their own ticket — closing is the team saying it is dealt with.
    dependencies=[Depends(require_permission("settings.write"))],
)
def close_ticket(
    ticket_id: str,
    payload: CloseTicketIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MyTicketRow:
    """Close a ticket with the resolution its reporter will read (#622).

    Tickets arrived ``open`` and stayed open forever: nothing could change a
    status. The close, and the audit row saying who closed it, how it was
    resolved and what it was before, commit together or not at all.
    """
    ensure_audit_table(db)
    ticket = db.get(SupportTicket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    if ticket.status == "closed":
        raise HTTPException(status_code=409, detail="ticket is already closed")
    resolution = payload.resolution_summary.strip()
    if not resolution:
        raise HTTPException(status_code=422, detail="a resolution is required")
    previous = ticket.status
    # Conditional, not read-then-write (#622 audit): two people closing the
    # same ticket at once both got 200, and the second resolution silently
    # replaced the first. Whoever loses the race gets the 409.
    closed = db.execute(
        update(SupportTicket)
        .where(SupportTicket.id == ticket_id, SupportTicket.status != "closed")
        .values(status="closed", closed_at=datetime.now(UTC), resolution_summary=resolution)
    ).rowcount
    if not closed:
        db.rollback()
        raise HTTPException(status_code=409, detail="ticket is already closed")
    audit_or_rollback(
        db,
        action="support_ticket_closed",
        entity_type="support_ticket",
        entity_id=str(ticket.id),
        actor=user,
        request=request,
        details={"subject": ticket.subject, "from_status": previous, "resolution_summary": resolution},
    )
    db.commit()
    db.refresh(ticket)
    log.info("support ticket closed — ticket=%s actor=%s", ticket.id, _caller_id(user) or "?")
    return _row(ticket, _user_emails(db, [ticket.opened_by_user_id]), detail=True)
