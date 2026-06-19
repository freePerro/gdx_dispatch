"""SS-25 Slice B — Starlette middleware for API versioning + deprecation.

Responsibilities:

1. Parse the ``Accept`` header via :mod:`gdx_dispatch.core.api_version` and stash
   the resolved version on ``request.state.api_version`` so downstream
   handlers can branch on it cheaply.
2. Reject malformed/unsupported vendor media types with ``HTTP 400``.
3. After the handler runs, consult the
   :class:`~gdx_dispatch.core.deprecation_registry.DeprecationRegistry`; if the
   current path is deprecated, inject ``Sunset`` and ``Deprecation``
   response headers per RFC 8594 and
   draft-ietf-httpapi-deprecation-header. If a replacement endpoint is
   known, also emit a ``Link: <...>; rel="successor-version"`` header
   per RFC 8288.

Integration wiring is deliberately left out — ``gdx_dispatch/main.py`` must
``add_middleware(APIVersioningMiddleware)`` at integration time. See
the INTEGRATION_TODO at the bottom of this module.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gdx_dispatch.core.api_version import (
    APIVersionError,
    ResolvedVersion,
    format_deprecation_header,
    format_sunset_header,
    resolve_version,
)
from gdx_dispatch.core.deprecation_registry import DeprecationRegistry, get_registry

logger = logging.getLogger(__name__)


class APIVersioningMiddleware(BaseHTTPMiddleware):
    """Parse Accept, enforce version, inject deprecation headers.

    Parameters
    ----------
    app:
        The ASGI app (Starlette/FastAPI) this middleware wraps.
    registry:
        Optional :class:`DeprecationRegistry`; defaults to the
        module-level singleton from
        :func:`gdx_dispatch.core.deprecation_registry.get_registry`. Injecting a
        registry is how tests supply synthetic entries.
    """

    def __init__(
        self,
        app,
        registry: DeprecationRegistry | None = None,
    ) -> None:
        super().__init__(app)
        self._registry = registry

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        accept = request.headers.get("accept")
        try:
            resolved = resolve_version(accept)
        except APIVersionError as exc:
            # A 400 here is the contract: the client opted into GDX
            # versioning and got it wrong. Silent fallback would hide
            # the bug until v2 ships and their "v9999" magically works.
            return JSONResponse(
                status_code=400,
                content={"detail": str(exc), "error": "invalid_api_version"},
            )

        # Stash on request.state for handlers. Kept as a ResolvedVersion
        # dataclass so callers can see both version and explicitness.
        request.state.api_version = resolved

        response = await call_next(request)

        self._maybe_inject_deprecation_headers(request, response, resolved)
        return response

    # ── internals ─────────────────────────────────────────────────────────

    def _registry_safe(self) -> DeprecationRegistry:
        if self._registry is not None:
            return self._registry
        return get_registry()

    def _maybe_inject_deprecation_headers(
        self,
        request: Request,
        response: Response,
        resolved: ResolvedVersion,
    ) -> None:
        try:
            registry = self._registry_safe()
        except Exception:  # noqa: BLE001 — fail-open for observability header set
            logger.exception("deprecation_registry_unavailable")
            return

        path = request.url.path
        entry = registry.lookup(path)
        if entry is None:
            return

        # Sunset per RFC 8594 — HTTP-date of the effective removal date.
        response.headers["Sunset"] = format_sunset_header(entry.sunset_at)
        # Deprecation per draft-ietf-httpapi-deprecation-header — the
        # date the surface was deprecated, or "true" if we don't know.
        response.headers["Deprecation"] = format_deprecation_header(entry.deprecated_at)

        if entry.replacement_endpoint:
            # RFC 8288 Link header naming the successor version.
            link = f'<{entry.replacement_endpoint}>; rel="successor-version"'
            existing = response.headers.get("Link")
            response.headers["Link"] = f"{existing}, {link}" if existing else link

        # Metrics counter would live here post-integration; resolved is
        # unused in this slice but preserved in the signature so future
        # policy (e.g. stricter rules on explicit-version callers) can
        # light up without a signature change.
        _ = resolved


# INTEGRATION_TODO(ss25-integration):
#   gdx_dispatch/main.py must add this middleware AFTER auth but BEFORE the
#   tracing middleware so `request.state.api_version` is populated when
#   handlers run:
#
#       from gdx_dispatch.core.middleware.api_versioning import APIVersioningMiddleware
#       app.add_middleware(APIVersioningMiddleware)
#
#   Order matters — do not place above the auth middleware or error
#   responses generated by auth won't carry Sunset headers for
#   deprecated authenticated endpoints.
