"""SS-32 slice E — SPIFFE authentication middleware.

Starlette middleware that ADDITIVELY authenticates requests presenting
a SPIFFE workload identity. Runs alongside the existing Bearer-token
flow — if no SPIFFE material is present, the request passes through
unchanged and downstream auth (``gdx_dispatch.core.auth``) handles it.

Accepted inputs (checked in order; first present wins):

1. ``request.state.peer_spiffe_id`` — set by an upstream mTLS layer
   (nginx ``$ssl_client_s_dn``, an ASGI mTLS terminator, or a test
   harness). Value is a raw SPIFFE ID string.
2. ``X-SPIFFE-SVID`` header containing a JWT-SVID.

Validation:

* JWT-SVID: full :func:`validate_jwt_svid` verification against the
  configured trust bundle + expected audiences.
* mTLS-derived ID: the trust layer has already validated the cert; we
  still parse the ID and require the trust domain to be one the trust
  bundle knows about (else we reject — an attacker-controlled proxy
  must not be able to inject a SPIFFE ID for a trust domain we don't
  recognise).

On success the middleware sets:

* ``request.state.agent_principal`` — :class:`AgentPrincipal` dataclass
  with spiffe_id, capabilities, tenant_scope, kind ("x509" | "jwt"),
  and source metadata.

Fail-closed semantics: if an SVID header / peer ID is PRESENT but fails
validation, the request is rejected with 401. We do NOT pass through —
presenting a bad SVID is an explicit authentication attempt and must
not silently fall back to anonymous.

TODO: not registered in ``gdx_dispatch/main.py``. The mount point
will sit BEFORE the SS-7 auth middleware so ``agent_principal`` is
available when ``auth.py`` composes a unified principal.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from gdx_dispatch.core.spiffe.spiffe_id import (
    SpiffeIdError,
    parse_spiffe_id,
)
from gdx_dispatch.core.spiffe.spire_trust_bundle import (
    TrustBundleCache,
    TrustBundleError,
)
from gdx_dispatch.core.spiffe.svid_validator import (
    JWTSVIDError,
    ValidatedSVID,
    validate_jwt_svid,
)
from gdx_dispatch.core.spiffe.workload_capability_map import (
    WorkloadCapabilityMap,
    resolve_capabilities,
)

logger = logging.getLogger(__name__)

SVID_HEADER = "x-spiffe-svid"


@dataclass(frozen=True)
class AgentPrincipal:
    """Composed principal for a SPIFFE-authenticated request."""

    spiffe_id: str
    trust_domain: str
    capabilities: tuple
    tenant_scope: str
    kind: str  # "x509" | "jwt"
    source: str  # "mtls" | "header"
    claims: Mapping[str, Any] = field(default_factory=dict)


class SPIFFEAuthMiddleware(BaseHTTPMiddleware):
    """Additive SPIFFE auth middleware."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        trust_bundle: TrustBundleCache,
        expected_audiences: Iterable[str],
        capability_map: Optional[WorkloadCapabilityMap] = None,
    ):
        super().__init__(app)
        self._bundle = trust_bundle
        self._audiences = tuple(expected_audiences)
        self._capability_map = capability_map

    async def dispatch(
        self, request: Request, call_next: Callable
    ) -> Response:
        peer_id = getattr(request.state, "peer_spiffe_id", None)
        header_svid = request.headers.get(SVID_HEADER)

        if not peer_id and not header_svid:
            # No SPIFFE material — pass through for Bearer auth.
            return await call_next(request)

        try:
            if peer_id:
                principal = self._authenticate_mtls(peer_id)
            else:
                principal = self._authenticate_jwt(header_svid)
        except _SPIFFEReject as exc:
            logger.warning("spiffe auth rejected: %s", exc.reason)
            return JSONResponse(
                {
                    "error": "spiffe_auth_failed",
                    "reason": exc.reason,
                },
                status_code=401,
            )

        request.state.agent_principal = principal
        return await call_next(request)

    # ------------------------------------------------------------------
    # mTLS flow
    # ------------------------------------------------------------------

    def _authenticate_mtls(self, raw_id: str) -> AgentPrincipal:
        try:
            sid = parse_spiffe_id(raw_id)
        except SpiffeIdError as exc:
            raise _SPIFFEReject(f"invalid peer SPIFFE ID: {exc}") from exc
        try:
            bundle = self._bundle.get()
        except TrustBundleError as exc:
            raise _SPIFFEReject(f"trust bundle unavailable: {exc}") from exc
        if sid.trust_domain not in bundle:
            raise _SPIFFEReject(
                f"unknown trust domain '{sid.trust_domain}'"
            )
        caps = self._resolve(sid.uri)
        return AgentPrincipal(
            spiffe_id=sid.uri,
            trust_domain=sid.trust_domain,
            capabilities=caps.capabilities,
            tenant_scope=caps.tenant_scope,
            kind="x509",
            source="mtls",
            claims={},
        )

    # ------------------------------------------------------------------
    # JWT-SVID flow
    # ------------------------------------------------------------------

    def _authenticate_jwt(self, token: str) -> AgentPrincipal:
        try:
            bundle = self._bundle.get()
        except TrustBundleError as exc:
            raise _SPIFFEReject(f"trust bundle unavailable: {exc}") from exc
        try:
            result: ValidatedSVID = validate_jwt_svid(
                token,
                trust_bundle=bundle,
                expected_audiences=self._audiences,
            )
        except JWTSVIDError as exc:
            raise _SPIFFEReject(f"jwt-svid invalid: {exc}") from exc
        caps = self._resolve(result.spiffe_id.uri)
        return AgentPrincipal(
            spiffe_id=result.spiffe_id.uri,
            trust_domain=result.spiffe_id.trust_domain,
            capabilities=caps.capabilities,
            tenant_scope=caps.tenant_scope,
            kind="jwt",
            source="header",
            claims=dict(result.claims),
        )

    def _resolve(self, uri: str):
        if self._capability_map is not None:
            return self._capability_map.resolve(uri)
        return resolve_capabilities(uri)


class _SPIFFEReject(Exception):
    """Internal signal — caught by middleware, translated to 401."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
