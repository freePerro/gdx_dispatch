"""Developer portal route — serves the developer.html template.

GET  /developer              → renders gdx_dispatch/templates/developer.html
GET  /api/api-keys           → list API keys for authenticated tenant
POST /api/api-keys           → create new scoped API key
DELETE /api/api-keys/{id}    → revoke (soft-delete) an API key
GET  /api/api-keys/{id}/usage → API call history / last-used info for key
"""
from __future__ import annotations

from typing import Any

import contextlib
import logging
import os
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

router = APIRouter(tags=["developer"])

_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "templates", "developer.html")


# ---------------------------------------------------------------------------
# Internal helpers — import lazily to avoid circular imports
# ---------------------------------------------------------------------------


def _get_api_keys_module():
    from gdx_dispatch.core import api_keys as ak
    return ak


def _get_db():
    from gdx_dispatch.core.api_keys import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def _require_authenticated_user(request: Request) -> dict:
    """Return current user dict from JWT. Raises 401 if unauthenticated."""
    try:
        from fastapi.security import OAuth2PasswordBearer

        from gdx_dispatch.routers.auth import get_current_user
        oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/login")
        token = await oauth2(request)
        return await get_current_user(request, token)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Authentication required") from None


class _CreateKeyBody(BaseModel):
    name: str
    scopes: list[str] = []
    expires_in_days: int | None = None


# ---------------------------------------------------------------------------
# Developer portal UI
# ---------------------------------------------------------------------------


@router.get("/developer", response_class=HTMLResponse, include_in_schema=False)
async def developer_portal() -> HTMLResponse:
    """Serve the API developer portal UI."""
    try:
        with open(os.path.abspath(_TEMPLATE_PATH), encoding="utf-8") as fh:
            html = fh.read()
    except FileNotFoundError:
        logging.getLogger(__name__).exception("developer_portal caught exception")
        html = "<h1>Developer portal template not found</h1>"
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# API key management routes
# ---------------------------------------------------------------------------


@router.get("/api/api-keys", summary="List API keys", tags=["developer"])
async def list_api_keys(
    request: Request,
    current_user: dict = Depends(_require_authenticated_user),
    db: Session = Depends(_get_db),
) -> JSONResponse:
    """List all API keys for the authenticated tenant (prefix only, never full key)."""
    ak = _get_api_keys_module()
    tenant_id_str = current_user.get("tenant_id", "")
    if not tenant_id_str:
        raise HTTPException(status_code=401, detail="tenant_id missing from token")
    try:
        tenant_uuid = UUID(tenant_id_str)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid tenant_id in token") from None

    try:
        keys = (
            db.query(ak.APIKey)
            .filter(ak.APIKey.tenant_id == tenant_uuid)
            .order_by(ak.APIKey.created_at.desc())
            .all()
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception("list_api_keys: api_keys table may not exist")
        with contextlib.suppress(Exception):
            db.rollback()
        return JSONResponse({"data": []})

    def _serialize(k: Any) -> dict:
        return {
            "id": str(k.id),
            "name": k.name,
            "key_prefix": k.key_prefix,
            "scopes": list(k.scopes or []),
            "created_at": k.created_at.isoformat() if k.created_at else None,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "expires_at": k.expires_at.isoformat() if k.expires_at else None,
            "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
        }

    return JSONResponse({"data": [_serialize(k) for k in keys]})


@router.post("/api/api-keys", status_code=201, summary="Create API key", tags=["developer"])
async def create_api_key(
    body: _CreateKeyBody,
    request: Request,
    current_user: dict = Depends(_require_authenticated_user),
    db: Session = Depends(_get_db),
) -> JSONResponse:
    """Create a new scoped API key. Returns full key ONCE — never shown again."""
    ak = _get_api_keys_module()
    tenant_id_str = current_user.get("tenant_id", "")
    if not tenant_id_str:
        raise HTTPException(status_code=401, detail="tenant_id missing from token")
    try:
        tenant_uuid = UUID(tenant_id_str)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid tenant_id in token") from None

    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    valid_scopes = {"read:jobs", "write:jobs", "read:customers", "write:customers"}
    invalid = set(body.scopes) - valid_scopes
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid scopes: {sorted(invalid)}. Valid: {sorted(valid_scopes)}",
        )

    from datetime import timedelta
    from uuid import uuid4
    raw_key, key_hash, key_prefix = ak.generate_api_key()
    expires_at = None
    if body.expires_in_days and body.expires_in_days > 0:
        expires_at = datetime.now(UTC) + timedelta(days=body.expires_in_days)

    api_key = ak.APIKey(
        id=uuid4(),
        tenant_id=tenant_uuid,
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=name,
        scopes=body.scopes,
        created_at=datetime.now(UTC),
        expires_at=expires_at,
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)

    return JSONResponse(
        status_code=201,
        content={
            "key": raw_key,
            "prefix": key_prefix,
            "id": str(api_key.id),
            "name": api_key.name,
            "scopes": list(api_key.scopes or []),
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
    )


@router.delete("/api/api-keys/{key_id}", status_code=200, summary="Revoke API key", tags=["developer"])
async def revoke_api_key(
    key_id: str,
    request: Request,
    current_user: dict = Depends(_require_authenticated_user),
    db: Session = Depends(_get_db),
) -> JSONResponse:
    """Soft-revoke an API key (sets revoked_at). Cannot be undone."""
    ak = _get_api_keys_module()
    tenant_id_str = current_user.get("tenant_id", "")
    if not tenant_id_str:
        raise HTTPException(status_code=401, detail="tenant_id missing from token")
    try:
        tenant_uuid = UUID(tenant_id_str)
        key_uuid = UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid UUID format") from None

    api_key = (
        db.query(ak.APIKey)
        .filter(ak.APIKey.id == key_uuid, ak.APIKey.tenant_id == tenant_uuid)
        .first()
    )
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    if api_key.revoked_at is not None:
        return JSONResponse({"ok": True, "message": "Already revoked"})

    api_key.revoked_at = datetime.now(UTC)
    db.commit()
    return JSONResponse({"ok": True, "id": key_id, "revoked_at": api_key.revoked_at.isoformat()})


@router.get("/api/api-keys/{key_id}/usage", summary="API key usage history", tags=["developer"])
async def api_key_usage(
    key_id: str,
    request: Request,
    current_user: dict = Depends(_require_authenticated_user),
    db: Session = Depends(_get_db),
) -> JSONResponse:
    """Return usage statistics for an API key (last_used_at, call count estimate)."""
    ak = _get_api_keys_module()
    tenant_id_str = current_user.get("tenant_id", "")
    if not tenant_id_str:
        raise HTTPException(status_code=401, detail="tenant_id missing from token")
    try:
        tenant_uuid = UUID(tenant_id_str)
        key_uuid = UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid UUID format") from None

    api_key = (
        db.query(ak.APIKey)
        .filter(ak.APIKey.id == key_uuid, ak.APIKey.tenant_id == tenant_uuid)
        .first()
    )
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    return JSONResponse({
        "data": {
            "id": str(api_key.id),
            "name": api_key.name,
            "key_prefix": api_key.key_prefix,
            "last_used_at": api_key.last_used_at.isoformat() if api_key.last_used_at else None,
            "created_at": api_key.created_at.isoformat() if api_key.created_at else None,
            "revoked_at": api_key.revoked_at.isoformat() if api_key.revoked_at else None,
            "scopes": list(api_key.scopes or []),
            # Usage log: last_used_at is the primary signal; call log table not yet implemented
            "usage_log": [],
        }
    })
