"""Client-side error capture (``/api/feedback/client-error``).

The in-app bug-report form posts to ``/api/support/bug`` (routers/support.py),
which is what the Feedback page reads back. The ``/bug-report`` POST and
``/bug-reports`` GET that lived here wrote a second copy to ``bug_reports``,
a table no screen ever read; removed 2026-09-06 (the physical table and its
rows stay).
"""
from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from gdx_dispatch.models.tenant_models import ClientError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/feedback", tags=["feedback"])


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

    Single-tenant: TenantMiddleware pins ``request.state.tenant`` on every
    non-bypassed path, so the write below normally runs. The tenantless
    branch (no tenant on request.state — a direct call, a test harness, or a
    bypassed path) logs to the server log and returns a distinct status
    string so a skipped write never looks like a successful one.
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

    # Tenant pinned — open one session on the application database directly
    # (not via Depends(get_db): this handler is reached on paths where the
    # dependency would not resolve). Single-tenant: SessionLocal IS that DB.
    from gdx_dispatch.core.database import SessionLocal  # noqa: PLC0415

    db = None
    try:
        db = SessionLocal()
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

