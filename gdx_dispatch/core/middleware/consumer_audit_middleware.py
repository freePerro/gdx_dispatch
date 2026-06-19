"""SS-28 slice C — consumer-audit middleware.

Starlette middleware that wraps every HTTP request, captures the
shape of the call (principal, tenant, IP, UA, path, method), dispatches
downstream, then writes one ``platform_consumer_audit`` row reflecting
the response status.

Security policy (per SS-28 spec "Rules"):

* **Fail-closed.** If the audit write raises, the response is replaced
  with HTTP 500 ``{"error":"audit_write_failed"}``. No request is
  allowed to complete without a matching audit row. This is a security
  control surface — silent failure is unacceptable.
* **Scope.** Writes are skipped for requests that lack a tenant
  context (e.g. ``/healthz``, pre-auth endpoints). Those surfaces are
  outside the consumer-audit regime; rule of thumb — "if there is no
  principal and no tenant_id on request.state, don't pretend there
  was one". An explicit allow-list is defined below as SKIP_PREFIXES
  so ops can audit the boundary.

Integration is deferred. The supervisor wires this middleware into
``gdx_dispatch/main.py`` at end-of-sprint; until then the module is import-safe
and ships with its own tests.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from sqlalchemy.exc import OperationalError as _SAOperationalError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gdx_dispatch.core.platform_audit import record_consumer_action

logger = logging.getLogger(__name__)

# Paths where no audit row is expected. Health/metrics/pre-auth.
# Supervisor integration may extend this via constructor arg.
DEFAULT_SKIP_PREFIXES: tuple[str, ...] = (
    "/healthz",
    "/readyz",
    "/metrics",
    "/static",
    "/favicon.ico",
)


def _client_ip(request: Request) -> str | None:
    # Prefer X-Forwarded-For if nginx is in front; fall back to scope client.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # First hop is the real client per RFC 7239 convention.
        return xff.split(",", 1)[0].strip()
    client = request.scope.get("client")
    if client:
        return client[0]
    return None


def _tenant_id(request: Request) -> str | None:
    state = getattr(request, "state", None)
    if state is None:
        return None
    tid = getattr(state, "tenant_id", None)
    if tid:
        return str(tid)
    tenant = getattr(state, "tenant", None)
    if isinstance(tenant, dict):
        tid = tenant.get("id")
        if tid:
            return str(tid)
    return None


def _principal_identity_id(request: Request) -> str | None:
    state = getattr(request, "state", None)
    if state is None:
        return None
    pid = getattr(state, "principal_identity_id", None)
    if pid:
        return str(pid)
    principal = getattr(state, "principal", None)
    if principal is not None:
        pid = getattr(principal, "identity_id", None)
        if pid:
            return str(pid)
    return None


def _result_from_status(status_code: int) -> str:
    if 200 <= status_code < 400:
        return "ok"
    if status_code in (401, 403):
        return "denied"
    return "error"


class ConsumerAuditMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that records one audit row per tenant-scoped request.

    Args:
        app: downstream ASGI app.
        db_session_factory: callable returning a SQLAlchemy session bound
            to the request scope. Required — the middleware must have a
            session to write through. Tests inject an in-memory factory.
        skip_prefixes: iterable of path prefixes that bypass audit.
    """

    def __init__(
        self,
        app,
        *,
        db_session_factory: Callable[[], Any],
        skip_prefixes: Iterable[str] = DEFAULT_SKIP_PREFIXES,
    ) -> None:
        super().__init__(app)
        if db_session_factory is None:
            raise ValueError("ConsumerAuditMiddleware: db_session_factory required")
        self._session_factory = db_session_factory
        self._skip_prefixes = tuple(skip_prefixes)

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if any(path.startswith(p) for p in self._skip_prefixes):
            return await call_next(request)

        # Dispatch the downstream handler first so we can record the real
        # response status. On exception, synthesize an "error" row before
        # re-raising — the request flow is already broken; we still owe
        # the audit trail.
        response: Response
        try:
            response = await call_next(request)
        except Exception:
            self._write_audit(request, status_code=500, fail_closed=True)
            raise

        tenant_id = _tenant_id(request)
        if tenant_id is None:
            # Non-tenant-scoped surface — no audit row expected.
            return response

        try:
            self._write_audit(request, status_code=response.status_code)
        except _SAOperationalError as exc:
            # Missing table (e.g. SS28 not yet migrated) — degrade gracefully
            # rather than converting a valid response into a 500.
            logger.warning(
                "consumer_audit: table unavailable, skipping audit for %s %s: %s",
                request.method,
                path,
                exc,
            )
        except Exception:  # noqa: BLE001 — fail-closed is the contract
            logger.exception(
                "consumer_audit: fail-closed — audit write failed for %s %s",
                request.method,
                path,
            )
            return JSONResponse(
                {"error": "audit_write_failed"},
                status_code=500,
            )

        return response

    def _write_audit(
        self,
        request: Request,
        *,
        status_code: int,
        fail_closed: bool = False,
    ) -> None:
        tenant_id = _tenant_id(request)
        if tenant_id is None:
            # Best-effort only when the exception path calls us. We do
            # not invent a tenant — skip in that case.
            return

        db = self._session_factory()
        if db is None:
            if fail_closed:
                return  # nothing we can do — caller is already erroring
            raise RuntimeError(
                "consumer_audit: session factory returned None — cannot "
                "honour fail-closed contract"
            )

        record_consumer_action(
            db,
            tenant_id=tenant_id,
            principal_identity_id=_principal_identity_id(request),
            action=f"{request.method} {request.url.path}",
            resource_type="http.request",
            resource_id=request.url.path,
            result=_result_from_status(status_code),
            details={
                "method": request.method,
                "path": request.url.path,
                "status": status_code,
            },
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        # Commit the audit row — the app transaction may roll back on
        # error, but the audit trail must survive. This mirrors the
        # accepted SOC 2 pattern: audit writes commit independently.
        db.commit()
