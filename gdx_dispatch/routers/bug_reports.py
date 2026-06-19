"""Bug Report — in-app bug reporting for users."""
from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import BugReport, ClientError
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/feedback", tags=["feedback"])


class BugReportIn(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=5, max_length=5000)
    priority: str = Field(default="medium", pattern="^(low|medium|high|critical)$")
    page_url: str | None = Field(default=None, max_length=500)
    browser_info: str | None = Field(default=None, max_length=500)


@router.post("/bug-report", status_code=201)
def create_bug_report(
    request: Request,
    payload: BugReportIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = str((getattr(request.state, "tenant", {}) or {}).get("id", ""))
    uid = str(user.get("sub") or user.get("user_id") or "system")
    report_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()

    report = BugReport(
        id=report_id,
        company_id=tid,
        user_id=uid,
        subject=payload.subject,
        description=payload.description,
        priority=payload.priority,
        page_url=payload.page_url,
        browser_info=payload.browser_info,
        status="new",
        created_at=now,
    )
    db.add(report)
    db.commit()

    log.info("Bug report created: %s — %s (priority: %s)", report_id, payload.subject, payload.priority)

    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="create",
        entity_type="bug_report", entity_id=report_id,
        details={"subject": payload.subject, "priority": payload.priority},
        request=request,
    )

    return {"status": "created", "id": report_id, "subject": payload.subject}


class ClientErrorIn(BaseModel):
    # Legacy API-error fields (sent by useApi.js on 4xx/5xx responses)
    url: str = Field(default="", max_length=500)
    method: str = Field(default="GET", max_length=10)
    status: int = Field(default=0)
    detail: str = Field(default="", max_length=2000)
    page: str = Field(default="", max_length=500)
    timestamp: str = Field(default="", max_length=50)

    # General-error fields (sent by errorCapture.js for window.onerror,
    # unhandled rejections, Vue errors, console.error, deprecations).
    # "kind" categorizes: window_error | unhandled_rejection | vue_error |
    # vue_warning | console_error | deprecation_warning
    kind: str = Field(default="api_error", max_length=40)
    source: str | None = Field(default=None, max_length=500)
    lineno: int | None = None
    colno: int | None = None
    stack: str | None = Field(default=None, max_length=2000)
    component: str | None = Field(default=None, max_length=200)
    info: str | None = Field(default=None, max_length=200)
    trace: str | None = Field(default=None, max_length=1000)
    user_agent: str | None = Field(default=None, max_length=200)


@router.post("/client-error")
def report_client_error(
    request: Request,
    payload: ClientErrorIn,
) -> dict[str, str]:
    """Receive frontend API errors for R&D tracking.

    MH-0 (2026-05-19): tenantless-safe. The route is in
    `TenantMiddleware._TENANTLESS_ALLOWED_PATHS` (NOT `_BYPASS_PATHS` —
    that was caught and rejected in the MH-0 audit because the bypass
    short-circuits before `_lookup_tenant` and would silently kill
    ClientError writes on real tenants). The middleware runs lookup
    normally and, only when the tenant cannot be resolved, sets
    `request.state.tenant = None` and lets the request through. Real
    tenant hosts still write to the tenant-plane `ClientError` table;
    platform/unresolved hosts hit the tenantless branch which logs to
    the server log and returns a distinct status string so a failed
    tenant write never looks like a successful one.
    """
    body = payload

    # Build the rich detail string once (same shape regardless of plane).
    parts = [f"[{body.kind}] {body.detail}".strip()]
    if body.source:
        loc = body.source
        if body.lineno is not None:
            loc += f":{body.lineno}"
            if body.colno is not None:
                loc += f":{body.colno}"
        parts.append(f"at {loc}")
    if body.component:
        parts.append(f"component={body.component}")
    if body.info:
        parts.append(f"info={body.info}")
    if body.stack:
        parts.append(f"\nstack: {body.stack[:800]}")
    elif body.trace:
        parts.append(f"\ntrace: {body.trace[:500]}")
    rich_detail = " ".join(p for p in parts if p)[:2000]

    tenant_state = getattr(request.state, "tenant", None) or {}
    tid = str(tenant_state.get("id", "") or "")

    if not tid:
        # Tenantless path — platform host or unresolved tenant. Log to the
        # server log so the event isn't lost. Don't try to write to a
        # tenant DB (there is none). Return 200 so the SPA's `keepalive`
        # fetch doesn't surface a console error and trip another capture.
        log.warning(
            "client_error_tenantless: kind=%s page=%s detail=%s",
            body.kind, body.page, rich_detail[:200],
        )
        return {"status": "logged_tenantless"}

    # Tenant resolved — open a tenant session manually (we can't Depends()
    # on get_db because the bypass means it would fail to even resolve
    # the request.state tenant in a few edge paths). Mirror the dep's logic:
    # decrypt db_url, get engine from registry, open one session.
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415
    from gdx_dispatch.core.database import _decrypt_db_url  # noqa: PLC0415
    from gdx_dispatch.core.tenant import engine_registry  # noqa: PLC0415

    db = None
    try:
        db_url = _decrypt_db_url(str(tenant_state["db_url"]))
        engine = engine_registry.get_engine(tid, db_url)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        db = session_factory()
        now = datetime.now(timezone.utc).isoformat()
        client_err = ClientError(
            id=str(uuid4()),
            company_id=tid,
            api_url=body.url,
            method=body.method,
            status_code=body.status,
            detail=rich_detail,
            page_url=body.page,
            created_at=now,
        )
        db.add(client_err)
        db.commit()
        log.info(
            "Client error logged: kind=%s page=%s detail=%s",
            body.kind, body.page, rich_detail[:200],
        )
    except Exception:
        log.exception("client_error_log_failed")
        if db is not None:
            with contextlib.suppress(Exception):
                db.rollback()
        # Audit round 2: distinct status so a decrypt/commit failure on
        # a real tenant doesn't look identical to a successful write. The
        # SPA doesn't read the body, but operators inspecting access logs
        # need to tell the two apart.
        return {"status": "logged_tenant_db_failed"}
    finally:
        if db is not None:
            with contextlib.suppress(Exception):
                db.close()

    return {"status": "logged"}


@router.get("/bug-reports")
def list_bug_reports(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection itself
    # (per-tenant DB). No company_id filter needed.
    reports = db.execute(
        select(BugReport)
        .order_by(BugReport.created_at.desc())
        .limit(100)
    ).scalars().all()
    return [
        {
            "id": r.id,
            "subject": r.subject,
            "description": r.description,
            "priority": r.priority,
            "status": r.status,
            "page_url": r.page_url,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in reports
    ]
