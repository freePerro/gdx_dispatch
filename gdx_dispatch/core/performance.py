from __future__ import annotations

import contextvars
import json
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.log_format import build_log_entry

router = APIRouter(prefix="/api/admin", tags=["performance"])

_request_ctx: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("perf_request_ctx", default=None)
_listeners_installed = False


class PerformanceLogger:
    def __init__(self) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=1000)
        self._pending_flush: list[dict[str, Any]] = []
        self._last_flush_at = datetime.now(UTC)

    def add_event(self, event_payload: dict[str, Any]) -> dict[str, Any]:
        self._events.append(event_payload)
        self._pending_flush.append(event_payload)
        return event_payload

    def log_slow_query(
        self,
        tenant_id: str,
        request_id: str,
        sql: str,
        params: Any,
        duration_ms: int,
        path: str | None = None,
    ) -> dict[str, Any]:
        entry = build_log_entry(
            level="WARNING",
            logger="gdx_dispatch.performance",
            request_id=request_id,
            tenant_id=tenant_id,
            action="slow_query",
            entity_type="query",
            duration_ms=int(duration_ms),
            details={"sql": sql, "params": params, "path": path or ""},
        )
        entry["event_type"] = "slow_query"
        entry["sql"] = sql
        entry["params"] = params
        entry["path"] = path or ""
        return self.add_event(entry)

    def log_slow_endpoint(self, tenant_id: str, request_id: str, path: str, duration_ms: int) -> dict[str, Any]:
        entry = build_log_entry(
            level="WARNING",
            logger="gdx_dispatch.performance",
            request_id=request_id,
            tenant_id=tenant_id,
            action="slow_endpoint",
            entity_type="endpoint",
            entity_id=path,
            duration_ms=int(duration_ms),
            details={"path": path},
        )
        entry["event_type"] = "slow_endpoint"
        entry["path"] = path
        return self.add_event(entry)

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._events)

    def flush_due(self, db: Session, interval_seconds: int = 60) -> int:
        ensure_performance_table(db)
        now = datetime.now(UTC)
        if (now - self._last_flush_at).total_seconds() < interval_seconds and self._pending_flush:
            return 0
        written = 0
        while self._pending_flush:
            event_row = self._pending_flush.pop(0)
            db.execute(
                text(
                    """
                    INSERT INTO performance_slow_events (
                        id, event_type, tenant_id, request_id, path, sql_text,
                        params_json, duration_ms, details, created_at
                    ) VALUES (
                        :id, :event_type, :tenant_id, :request_id, :path, :sql_text,
                        :params_json, :duration_ms, :details, :created_at
                    )
                    """
                ),
                {
                    "id": f"perf-{written}-{int(time.time() * 1000)}",
                    "event_type": event_row.get("event_type"),
                    "tenant_id": event_row.get("tenant_id") or "-",
                    "request_id": event_row.get("request_id") or "-",
                    "path": event_row.get("path") or "",
                    "sql_text": event_row.get("sql") or "",
                    "params_json": json.dumps(event_row.get("params") or {}),
                    "duration_ms": int(event_row.get("duration_ms") or 0),
                    "details": json.dumps(event_row.get("details") or {}),
                    "created_at": event_row.get("timestamp"),
                },
            )
            written += 1
        db.commit()
        self._last_flush_at = now
        return written


_performance_logger_singleton: PerformanceLogger | None = None


def get_performance_logger() -> PerformanceLogger:
    global _performance_logger_singleton
    if _performance_logger_singleton is None:
        _performance_logger_singleton = PerformanceLogger()
    return _performance_logger_singleton


def reset_performance_logger() -> None:
    global _performance_logger_singleton
    _performance_logger_singleton = PerformanceLogger()


def _require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if str(user.get("role") or "") not in {"admin", "owner", "superadmin"}:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def ensure_performance_table(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS performance_slow_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                request_id TEXT,
                path TEXT,
                sql_text TEXT,
                params_json TEXT,
                duration_ms INTEGER NOT NULL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    db.commit()


def _install_sql_listeners_once(threshold_ms: int) -> None:
    global _listeners_installed
    if _listeners_installed:
        return

    @event.listens_for(Engine, "before_cursor_execute")
    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        context._gdx_query_started_at = time.perf_counter()
        context._gdx_statement = statement
        context._gdx_parameters = parameters

    @event.listens_for(Engine, "after_cursor_execute")
    def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        started = getattr(context, "_gdx_query_started_at", None)
        if started is None:
            return
        duration_ms = int((time.perf_counter() - started) * 1000)
        if duration_ms <= threshold_ms:
            return
        ctx = _request_ctx.get({})
        get_performance_logger().log_slow_query(
            tenant_id=str(ctx.get("tenant_id", "-")),
            request_id=str(ctx.get("request_id", "-")),
            sql=str(getattr(context, "_gdx_statement", statement)),
            params=getattr(context, "_gdx_parameters", parameters),
            duration_ms=duration_ms,
            path=str(ctx.get("path", "")),
        )

    _listeners_installed = True


class SlowQueryMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, threshold_ms: int = 500) -> None:
        super().__init__(app)
        self.threshold_ms = max(1, int(threshold_ms))
        _install_sql_listeners_once(self.threshold_ms)

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        tenant_id = str(getattr(request.state, "tenant", {}).get("id", request.headers.get("x-tenant-id", "-")))
        request_id = str(getattr(request.state, "request_id", request.headers.get("x-request-id", "-")))
        token = _request_ctx.set({"tenant_id": tenant_id, "request_id": request_id, "path": request.url.path})
        try:
            return await call_next(request)
        finally:
            _request_ctx.reset(token)


class SlowEndpointMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, threshold_ms: int = 3000) -> None:
        super().__init__(app)
        self.threshold_ms = max(1, int(threshold_ms))

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = int((time.perf_counter() - started) * 1000)
        if duration_ms > self.threshold_ms:
            tenant_id = str(getattr(request.state, "tenant", {}).get("id", request.headers.get("x-tenant-id", "-")))
            request_id = str(getattr(request.state, "request_id", request.headers.get("x-request-id", "-")))
            get_performance_logger().log_slow_endpoint(
                tenant_id=tenant_id,
                request_id=request_id,
                path=request.url.path,
                duration_ms=duration_ms,
            )
        return response


@router.get("/performance")
def get_performance_dashboard(
    _: dict[str, Any] = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_performance_table(db)
    logger = get_performance_logger()
    logger.flush_due(db, interval_seconds=0)
    threshold = (datetime.now(UTC) - timedelta(hours=24)).isoformat()

    rows = db.execute(
        text(
            """
            SELECT event_type, tenant_id, request_id, path, sql_text, duration_ms, created_at
            FROM performance_slow_events
            WHERE created_at >= :threshold
            ORDER BY duration_ms DESC
            LIMIT 200
            """
        ),
        {"threshold": threshold},
    ).mappings().all()

    slow_queries = [
        {
            "tenant_id": r.get("tenant_id"),
            "request_id": r.get("request_id"),
            "sql": r.get("sql_text"),
            "duration_ms": int(r.get("duration_ms") or 0),
            "created_at": r.get("created_at"),
        }
        for r in rows
        if r.get("event_type") == "slow_query"
    ][:50]

    slow_endpoints = [
        {
            "tenant_id": r.get("tenant_id"),
            "request_id": r.get("request_id"),
            "path": r.get("path"),
            "duration_ms": int(r.get("duration_ms") or 0),
            "created_at": r.get("created_at"),
        }
        for r in rows
        if r.get("event_type") == "slow_endpoint"
    ][:50]

    return {
        "window": "last_24h",
        "slow_queries": slow_queries,
        "slow_endpoints": slow_endpoints,
        "in_memory_events": logger.snapshot()[-20:],
    }
