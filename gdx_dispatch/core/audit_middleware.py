from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from gdx_dispatch.core.audit import log_audit_event
from gdx_dispatch.core.database import SessionLocal as _AppSessionLocal


def _client_ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",", 1)[0].strip()
    return request.client.host if request.client else None


class AuditMiddleware(BaseHTTPMiddleware):
    """Capture auth lifecycle events (login/logout/failed_login)."""

    def __init__(self, app: Any, session_factory: Callable[[], Session] | None = None) -> None:
        super().__init__(app)
        self._session_factory = session_factory

    def _resolve_session_factory(self, request: Request) -> Callable[[], Session] | None:
        if self._session_factory is not None:
            return self._session_factory

        sf = getattr(request.app.state, "audit_db_session_factory", None)
        if callable(sf):
            return sf

        return _AppSessionLocal

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        method = request.method.upper()

        response = await call_next(request)

        if method != "POST":
            return response

        action: str | None = None
        if path == "/auth/login" and response.status_code < 400:
            action = "login"
        elif path == "/auth/logout" and response.status_code < 400:
            action = "logout"
        elif path.startswith("/auth/login") and response.status_code >= 400:
            action = "failed_login"

        if action is None:
            return response

        sf = self._resolve_session_factory(request)
        if sf is None:
            return response

        db = sf()
        try:
            tenant = getattr(request.state, "tenant", {}) or {}
            tenant_id = str(tenant.get("id", "")) or None
            await log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id="anonymous",
                action=action,
                entity_type="auth",
                entity_id="session",
                details={"path": path, "status_code": response.status_code},
                ip_address=_client_ip(request),
                request_id=getattr(request.state, "request_id", None) or request.headers.get("x-request-id"),
            )
            db.commit()
        except Exception:
            logging.getLogger(__name__).exception("dispatch caught exception")
            db.rollback()
        finally:
            db.close()

        return response
