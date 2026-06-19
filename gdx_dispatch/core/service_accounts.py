"""Platform-internal service account authentication.

Service accounts are distinct from tenant-scoped api_keys:
- Live in the control DB (`service_accounts` table)
- Cross-tenant capable (allowed_tenant_uuids array, null = all tenants)
- Authenticated via `X-Service-Key` header (prefix `svc_live_`)
- Actions audit-logged with `actor_type=service_account` + name
- Never visible to tenants; platform-internal only
- Minted by CLI (gdx_dispatch/tools/service_account_mint.py) — no web UI

Middleware behavior:
- If `X-Service-Key` header is present, authenticate against control DB
- On success: set request.state.current_user to an admin-equivalent dict with
  `service_account=True` + scopes + allowed_tenant_uuids, so existing
  `require_role("admin")` gates pass transparently for permitted endpoints
- Scope enforcement: `allowed_scopes` supports exact match and wildcards
  (e.g., `read:*` matches any `read:<anything>` requirement). If the scanner
  only needs reads, mint it with `["read:*"]` and it cannot mutate.
- Tenant enforcement: if the request hits a tenant endpoint, verify the
  resolved tenant UUID is in the SA's allowed_tenant_uuids (or allowed=null)
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

KEY_PREFIX = "svc_live_"
KEY_HEADER = "X-Service-Key"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, str, str]:
    """Returns (full_key, key_hash, key_prefix). Full key is shown to the
    operator ONCE at mint time and never stored in plaintext.

    Prefix is capped at 16 chars total (9-char KEY_PREFIX + 7 random) to fit
    the key_prefix String(16) column. Prefix is a log-identifier only, not
    a credential — the full raw key (50+ chars) carries the entropy."""
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_key(raw), raw[:16]


def scope_matches(required: str, granted: list[str]) -> bool:
    """Match required scope against granted scopes. Supports wildcard suffix.
    e.g. required='read:jobs', granted=['read:*'] → match. Exact match also OK."""
    if required in granted:
        return True
    for g in granted:
        if g.endswith(":*"):
            prefix = g[:-1]  # e.g. 'read:'
            if required.startswith(prefix):
                return True
        if g == "*":
            return True
    return False


def lookup_service_account(key_hash: str, control_db: Session):
    """Returns the ServiceAccount ORM row if the hash matches an un-revoked
    service account, else None. Caller updates last_used_at."""
    from gdx_dispatch.control.models import ServiceAccount

    row = control_db.execute(
        select(ServiceAccount).where(
            ServiceAccount.key_hash == key_hash,
            ServiceAccount.revoked_at.is_(None),
        )
    ).scalar_one_or_none()
    return row


def tenant_allowed(sa_row, tenant_uuid: str | None) -> bool:
    """True if this service account is permitted to act on this tenant.

    Matches against ``allowed_tenant_uuids`` (UUID strings). None → any tenant.
    """
    allowed_uuids = sa_row.allowed_tenant_uuids
    if allowed_uuids is None:
        return True
    if tenant_uuid is None:
        # Service account hitting a non-tenant endpoint (e.g. /health, /auth/*)
        return True
    return tenant_uuid in allowed_uuids


class ServiceKeyMiddleware(BaseHTTPMiddleware):
    """Authenticates requests bearing `X-Service-Key`. Falls through to
    downstream auth (JWT, cookie session) if the header is absent.

    Sets on success:
      request.state.current_user = {
          "role": "admin",  # so require_role("admin") gates pass
          "email": f"svc:{sa.name}",
          "sub": str(sa.id),
          "service_account": True,
          "service_account_name": sa.name,
          "scopes": sa.allowed_scopes,
          "allowed_tenant_uuids": sa.allowed_tenant_uuids,
      }
      request.state.actor_type = "service_account"
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        key = request.headers.get(KEY_HEADER, "")
        if not key:
            return await call_next(request)
        if not key.startswith(KEY_PREFIX):
            return JSONResponse({"detail": "Invalid service key format"}, status_code=401)

        try:
            from gdx_dispatch.core.database import SessionLocal
        except Exception:
            logging.getLogger(__name__).exception("dispatch caught exception")
            return JSONResponse({"detail": "Service key auth unavailable"}, status_code=503)

        if SessionLocal is None:
            return JSONResponse({"detail": "Service key auth unavailable"}, status_code=503)

        with SessionLocal() as cdb:
            sa_row = lookup_service_account(hash_key(key), cdb)
            if sa_row is None:
                return JSONResponse({"detail": "Invalid or revoked service key"}, status_code=401)
            # Update last_used_at (best-effort, don't block on failure)
            try:
                sa_row.last_used_at = _utcnow()
                cdb.commit()
            except Exception:
                logging.getLogger(__name__).exception("dispatch caught exception")
                cdb.rollback()

            request.state.current_user = {
                "role": "admin",
                "email": f"svc:{sa_row.name}",
                "sub": str(sa_row.id),
                "user_id": str(sa_row.id),
                "service_account": True,
                "service_account_name": sa_row.name,
                "scopes": list(sa_row.allowed_scopes or []),
                "allowed_tenant_uuids": list(sa_row.allowed_tenant_uuids) if sa_row.allowed_tenant_uuids else None,
            }
            request.state.actor_type = "service_account"
            request.state.service_account_id = str(sa_row.id)

            # Enforce tenant allowlist against the tenant TenantMiddleware already resolved.
            # D97: allowlist is matched against UUID, not slug.
            tenant = getattr(request.state, "tenant", None) or {}
            tenant_uuid = tenant.get("id")
            tenant_slug = tenant.get("slug")  # for error messages only
            if not tenant_allowed(sa_row, tenant_uuid):
                return JSONResponse(
                    {"detail": f"Service account '{sa_row.name}' is not allowed on tenant '{tenant_slug}'"},
                    status_code=403,
                )

        return await call_next(request)
