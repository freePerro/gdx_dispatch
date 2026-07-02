"""Global error handler middleware — catches all unhandled exceptions and returns proper JSON."""
from __future__ import annotations

import logging
import traceback
from collections import defaultdict

from fastapi import Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("gdx_dispatch.error_handler")


async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI exception handler registered via app.add_exception_handler().

    Provides structured JSON error responses for all exception types and
    persists 5xx + non-HTTPException failures to the self-hosted error
    sink (UX audit F-18 / 2026-04-29 — replaces Sentry)."""
    request_id = getattr(request.state, "request_id", "unknown") if hasattr(request, "state") else "unknown"

    if isinstance(exc, HTTPException):
        # Sink server-side failures only (5xx, excluding 503). 4xx are NOT
        # sinked: they are expected client-side outcomes, not server errors.
        # In particular 403 ("permission denied") was previously recorded on
        # the theory it flagged auth misconfig, but in practice it is dominated
        # by ordinary RBAC denials (a role correctly hitting an endpoint it
        # can't use) — it was the single largest source of Server-Errors-page
        # noise. 401 is token-refresh/expiry noise (hundreds/hour) and
        # 400/404/422 are client validation noise. 503 is excluded because in
        # this codebase it's the convention for "feature not configured"
        # (Google/Microsoft SSO, legacy /api/superadmin, /legacy/billing — all
        # 503 by design when the relevant env var is absent). Real DB-down
        # 503s come through the non-HTTPException branch below
        # (OperationalError → 503) and remain logged.
        if exc.status_code >= 500 and exc.status_code != 503:
            _sink(request, exc, exc.status_code, request_id)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "request_id": request_id},
        )

    exc_name = type(exc).__name__
    status = 500
    message = "Internal server error"

    if "IntegrityError" in exc_name:
        status = 409
        message = "Data conflict"
    elif "OperationalError" in exc_name:
        status = 503
        message = "Database temporarily unavailable"
    elif isinstance(exc, (ValueError, TypeError)):
        status = 400
        message = str(exc)[:200]
    elif isinstance(exc, KeyError):
        status = 400
        message = f"Missing field: {exc}"

    log.error("global_exception path=%s error=%s: %s", request.url.path, exc_name, str(exc)[:300])

    # Persist to the self-hosted sink for any 5xx outcome. Best-effort —
    # the sink swallows its own errors so the response always reaches the
    # client even if the control DB is down.
    if status >= 500:
        _sink(request, exc, status, request_id)

    return JSONResponse(
        status_code=status,
        content={"detail": message, "request_id": request_id, "error_type": exc_name},
    )


def _sink(request: Request, exc: BaseException, status_code: int, request_id: str) -> None:
    """Lazy import to avoid a circular dep at module load time and to
    keep the sink optional during tests / cold start."""
    try:
        from gdx_dispatch.modules.error_sink import record_server_error
        record_server_error(request=request, exc=exc, status_code=status_code, request_id=request_id)
    except Exception:  # noqa: BLE001
        log.exception("error_sink_dispatch_failed")


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """Catches unhandled exceptions and returns structured JSON errors.

    Prevents HTML stacktraces from leaking to clients. Logs every error
    with request context for debugging. Tracks error rates per endpoint.
    """

    def __init__(self, app):
        super().__init__(app)
        self.error_counts: dict[str, int] = defaultdict(int)

    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:
            logging.getLogger(__name__).exception("dispatch caught exception")
            return self._handle(request, exc)

    def _handle(self, request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unknown") if hasattr(request, "state") else "unknown"
        tenant = getattr(request.state, "tenant", {}) if hasattr(request, "state") else {}
        tenant_id = str((tenant or {}).get("id", ""))
        path = request.url.path
        method = request.method

        self.error_counts[f"{method} {path}"] += 1

        status = 500
        message = "Internal server error"
        exc_name = type(exc).__name__

        if "IntegrityError" in exc_name:
            status = 409
            message = "Data conflict — a record with this data already exists"
        elif "OperationalError" in exc_name:
            status = 503
            message = "Database temporarily unavailable"
        elif "ProgrammingError" in exc_name:
            status = 500
            message = "Database schema error"
        elif isinstance(exc, (ValueError, TypeError)):
            status = 400
            message = str(exc)[:200]
        elif isinstance(exc, PermissionError):
            status = 403
            message = "Permission denied"
        elif isinstance(exc, KeyError):
            status = 400
            message = f"Missing required field: {exc}"
        elif isinstance(exc, FileNotFoundError):
            status = 404
            message = "Resource not found"
        elif isinstance(exc, TimeoutError):
            status = 504
            message = "Request timed out"
        elif "StripeError" in exc_name:
            status = 502
            message = "Payment provider error"

        log.error(
            "unhandled_exception",
            extra={
                "request_id": request_id,
                "tenant_id": tenant_id,
                "path": path,
                "method": method,
                "status": status,
                "exception": exc_name,
                "error_message": str(exc)[:500],
                "traceback": traceback.format_exc()[-2000:],
            },
        )

        # Persist server-side faults to the control-plane sink so they
        # surface on the CC dashboard (/cockpit/support/errors).
        # Exceptions caught HERE propagate through the middleware stack
        # and never reached the FastAPI exception handler, so before this
        # they were container-log-only and invisible to operators — the
        # blind spot that hid the 6-day job-save outage (2026-05-19).
        # Gate follows global_exception_handler's (>=500, excl. 503 for
        # transient DB-unavailable). KNOWN RESIDUAL: _handle maps
        # TypeError/KeyError -> 400, so a genuine server bug surfacing as
        # one of those is still downgraded and stays invisible here. That
        # mapping is pre-existing and out of scope for this hotfix.
        # Best-effort: _sink has its own try/except + own control session
        # and never blocks or delays the error response beyond the bound
        # control-pool timeout.
        if status >= 500 and status != 503:
            _sink(request, exc, status, request_id)

        return JSONResponse(
            status_code=status,
            content={
                "detail": message,
                "request_id": request_id,
                "error_type": exc_name,
            },
        )
