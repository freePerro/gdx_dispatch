from __future__ import annotations

import logging
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("gdx_dispatch.requests")


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())[:8]
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state["request_id"] = request_id
        start = time.time()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = int((time.time() - start) * 1000)
            tenant_data = state.get("tenant", {}) if isinstance(state, dict) else {}
            tenant_id = tenant_data.get("id", "-") if isinstance(tenant_data, dict) else "-"

            log.info(
                "request_complete",
                extra={
                    "request_id": request_id,
                    "tenant_id": tenant_id,
                    "method": scope.get("method", ""),
                    "path": scope.get("path", ""),
                    "status": status_code,
                    "duration_ms": duration_ms,
                },
            )
