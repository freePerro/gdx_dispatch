"""SS-11 Slice A — platform tracing-tag middleware.

``PlatformTracingMiddleware`` enriches the active OpenTelemetry request
span with platform context fields harvested from ``request.state``:

* ``gdx.tenant_id`` — from ``request.state.tenant["id"]`` (or an object
  with an ``id`` attribute).
* ``gdx.installation_id`` — from
  ``request.state.principal.installation_id``.
* ``gdx.acting_on_tenant_id`` — from
  ``request.state.acting_on_tenant_id``.

Failure policy is **fail-open**: missing state, missing fields, a
non-recording span, or a raised exception while extracting attributes
must not interrupt the request. Tracing tags are observability metadata,
not a correctness invariant of the response.

This slice is strictly tagging-only — policy/auth child spans belong to
SS-11 Slice B+.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from opentelemetry import trace
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

_ATTR_TENANT_ID = "gdx_dispatch.tenant_id"
_ATTR_INSTALLATION_ID = "gdx_dispatch.installation_id"
_ATTR_ACTING_ON_TENANT_ID = "gdx_dispatch.acting_on_tenant_id"


class PlatformTracingMiddleware(BaseHTTPMiddleware):
    """Attach platform context fields to the active OTel request span."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        try:
            self._tag_active_span(request)
        except Exception:  # noqa: BLE001 — fail-open by design
            logger.exception("platform_tracing_middleware_failure")
        return await call_next(request)

    # ── internals ──────────────────────────────────────────────────────────

    @staticmethod
    def _tag_active_span(request: Request) -> None:
        span = trace.get_current_span()
        if span is None or not span.is_recording():
            return

        tenant_id = _extract_tenant_id(request)
        if tenant_id is not None:
            span.set_attribute(_ATTR_TENANT_ID, tenant_id)

        installation_id = _extract_installation_id(request)
        if installation_id is not None:
            span.set_attribute(_ATTR_INSTALLATION_ID, installation_id)

        acting_on = _extract_acting_on_tenant_id(request)
        if acting_on is not None:
            span.set_attribute(_ATTR_ACTING_ON_TENANT_ID, acting_on)


def _extract_tenant_id(request: Request) -> str | None:
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        return None
    raw: Any = tenant.get("id") if isinstance(tenant, dict) else getattr(tenant, "id", None)
    return _stringify(raw)


def _extract_installation_id(request: Request) -> str | None:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        return None
    return _stringify(getattr(principal, "installation_id", None))


def _extract_acting_on_tenant_id(request: Request) -> str | None:
    return _stringify(getattr(request.state, "acting_on_tenant_id", None))


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return text
