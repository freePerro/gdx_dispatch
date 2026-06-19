"""SCIM Bearer token auth scheme (SS-22 slice B).

Standalone from ``gdx_dispatch.core.auth`` by design — SCIM credentials are
tenant-scoped enterprise IdP tokens (Okta / Azure AD / OneLogin) and must
NEVER be mixed with the normal session/JWT stack. A SCIM token presented
to a non-SCIM route must not authenticate, and a normal session cookie on
a SCIM route must not authenticate either. Keeping the dependency
separate enforces that at the import graph level.

Token storage lookup is abstracted behind ``_load_scim_token_record``:
the default implementation reads a JSON blob from the ``GDX_SCIM_TOKENS``
env var so tests and dev can configure tokens without standing up a full
provisioning pipeline. INTEGRATION TODO: replace with a tenant-scoped
AccessToken row lookup using structured capabilities once SS-14 PAT
issuance lands (see SS-22 plan v3 patch P38).

All error responses use the RFC 7644 SCIM error schema.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse

_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


@dataclass(frozen=True)
class ScimPrincipal:
    """The authenticated SCIM caller for the current request."""

    token_id: str
    tenant_id: str
    capabilities: tuple[str, ...]


class ScimAuthError(Exception):
    """Raised by the SCIM auth dependency chain; converted to a SCIM-shaped
    JSON response by the router's exception handler."""

    def __init__(self, detail: str, http_status: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.http_status = http_status


def scim_error_body(detail: str, http_status: int) -> dict:
    """Build the canonical RFC 7644 error response body."""
    return {
        "schemas": [_ERROR_SCHEMA],
        "status": str(http_status),
        "detail": detail,
    }


def scim_error_response(detail: str, http_status: int) -> JSONResponse:
    """Emit a SCIM-shaped error response directly."""
    return JSONResponse(
        status_code=http_status,
        content=scim_error_body(detail, http_status),
    )


def _load_scim_token_record(token: str) -> dict | None:
    """Resolve a bearer token to its metadata record.

    Default: read ``GDX_SCIM_TOKENS`` as JSON mapping
    ``{token_string: {"tenant_id": "...", "capabilities": [...]}}``.
    Returns None when the token is unknown.
    """
    raw = os.getenv("GDX_SCIM_TOKENS", "")
    if not raw.strip():
        return None
    try:
        table = json.loads(raw)
    except json.JSONDecodeError:
        return None
    record = table.get(token)
    if record is None or not isinstance(record, dict):
        return None
    return record


def require_scim_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> ScimPrincipal:
    """FastAPI dependency: validate a ``Bearer <token>`` header for SCIM."""
    if not authorization:
        raise ScimAuthError(
            "Missing Authorization header; SCIM requires Bearer token.",
            401,
        )

    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise ScimAuthError(
            "Malformed Authorization header; expected 'Bearer <token>'.",
            401,
        )

    token = parts[1].strip()

    # Allow tests to inject a token table onto request.app.state without
    # having to roundtrip through GDX_SCIM_TOKENS. Production path still
    # consults the env var.
    table = getattr(request.app.state, "scim_tokens", None)
    record: dict | None
    if isinstance(table, dict):
        raw_record = table.get(token)
        record = raw_record if isinstance(raw_record, dict) else None
    else:
        record = _load_scim_token_record(token)

    if record is None:
        raise ScimAuthError("Invalid or unknown SCIM bearer token.", 401)

    tenant_id = record.get("tenant_id")
    if not tenant_id or not isinstance(tenant_id, str):
        raise ScimAuthError("SCIM token is not bound to a tenant.", 401)

    caps_raw = record.get("capabilities") or []
    if not isinstance(caps_raw, list):
        raise ScimAuthError("SCIM token capabilities are malformed.", 401)
    capabilities = tuple(str(c) for c in caps_raw)

    return ScimPrincipal(
        token_id=(token[:8] + "…") if len(token) > 8 else token,
        tenant_id=tenant_id,
        capabilities=capabilities,
    )


def require_scim_capability(capability: str):
    """Build a dependency that enforces a specific capability string.

    SS-22 uses colon-flattened structured capabilities
    (e.g. ``"write:identity"``) — the platform's structured
    ``(action, resource_type)`` model translated to a token-embeddable
    form. See SS-22 plan v3 patch P38.
    """

    def _check(
        principal: ScimPrincipal = Depends(require_scim_auth),
    ) -> ScimPrincipal:
        if capability not in principal.capabilities:
            raise ScimAuthError(
                f"SCIM token missing required capability: {capability}",
                403,
            )
        return principal

    return _check
