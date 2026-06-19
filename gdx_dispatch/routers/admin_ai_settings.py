"""Sprint 1.x-S26 — admin AI settings router (key set/test/clear).

Auth: uses ``gdx_dispatch.routers.auth.get_current_user`` (the SS-7 SPA-compatible
path) rather than ``gdx_dispatch.core.auth_dispatcher.get_current_principal`` (the
strict OAuth path). Reason: the SPA login at ``/auth/login`` mints a JWT
that is sent as ``Authorization: Bearer <jwt>``; the auth_dispatcher
treats every JWT-shaped Bearer token as an OAuth lookup and rejects it
with ``invalid_oauth_token``. ``get_current_user`` validates the JWT via
SS-7 ``validate_principal`` which is what the existing admin routers
(``audit``, ``admin_ops``) use.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from sqlalchemy import text

from gdx_dispatch.control.models import TenantSettings
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.llm.key_storage import clear_key, set_key, test_the_key
from gdx_dispatch.routers.auth import get_current_user


router = APIRouter(prefix="/api/admin/ai-settings", tags=["admin", "ai"])


def get_admin_principal_for_ai_settings(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Allow admin/owner/super-admin only. Wrapper so tests can override."""
    role = (user.get("role") or "").lower()
    if role not in ("admin", "owner", "superadmin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin only",
        )
    return user


def get_db_for_ai_settings(db: Session = Depends(get_db)) -> Session:
    """Wrapper so tests can override the control-plane Session dep."""
    return db


def _coerce_tenant_uuid(user: dict[str, Any]) -> UUID:
    """Pull the tenant_id from the user dict and coerce to UUID."""
    tid = user.get("tenant_id")
    if not tid:
        raise HTTPException(status_code=400, detail="missing tenant context")
    return tid if isinstance(tid, UUID) else UUID(str(tid))


def _coerce_user_id(user: dict[str, Any]) -> str:
    # get_current_user (SS-7 SPA path) returns {"user_id": ...}; older code paths
    # use "id" or "sub". Check all three so the audit row records the real admin
    # instead of "unknown" (P2-3 fix 2026-04-27).
    return str(user.get("user_id") or user.get("id") or user.get("sub") or "unknown")


def get_settings_state(db: Session, tenant_id) -> dict[str, Any]:
    """Return the public-safe view of TenantSettings — never the key itself."""
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    settings = db.get(TenantSettings, tid)
    if settings is None:
        return {"key_set": False, "last_validated_at": None, "last_error": None}
    return {
        "key_set": settings.llm_provider_key_enc is not None,
        "last_validated_at": (
            settings.llm_provider_key_last_validated_at.isoformat()
            if settings.llm_provider_key_last_validated_at
            else None
        ),
        "last_error": settings.llm_provider_key_last_error,
    }


class UpdateKeyRequest(BaseModel):
    key: str = Field(..., min_length=10, max_length=4000)


@router.get("")
def get_ai_settings(
    user: dict[str, Any] = Depends(get_admin_principal_for_ai_settings),
    db: Session = Depends(get_db_for_ai_settings),
) -> dict[str, Any]:
    return get_settings_state(db, _coerce_tenant_uuid(user))


@router.put("")
def put_ai_settings(
    payload: UpdateKeyRequest,
    user: dict[str, Any] = Depends(get_admin_principal_for_ai_settings),
    db: Session = Depends(get_db_for_ai_settings),
) -> dict[str, Any]:
    tid = _coerce_tenant_uuid(user)
    set_key(db, tid, payload.key, user_id=_coerce_user_id(user))
    return test_the_key(db, tid)


@router.delete("")
def delete_ai_settings(
    user: dict[str, Any] = Depends(get_admin_principal_for_ai_settings),
    db: Session = Depends(get_db_for_ai_settings),
) -> dict[str, Any]:
    tid = _coerce_tenant_uuid(user)
    clear_key(db, tid, user_id=_coerce_user_id(user))
    return {"cleared": True}


def _fetch_ai_audit_rows(
    db: Session, tenant_id: UUID, *, limit: int = 10
) -> list[dict[str, Any]]:
    """Fetch recent AI settings audit logs from the control-plane audit_logs.

    Uses raw SQL to select only the columns that exist in the control-plane
    schema. The tenant-plane ``AuditLog`` ORM model includes columns
    (``request_id``, ...) that the control-plane table does not have, so a
    full ORM SELECT raises ``UndefinedColumn``. Each row is returned as a
    dict matching the ``_audit_row_to_dict`` shape.
    """
    rows = db.execute(
        text(
            """
            SELECT id, action, user_id, details, created_at
            FROM audit_logs
            WHERE tenant_id = :tenant_id
              AND action LIKE 'ai_settings.%'
            ORDER BY created_at DESC, id DESC
            LIMIT :limit
            """
        ),
        {"tenant_id": str(tenant_id), "limit": limit},
    ).all()
    return [
        {
            "id": str(r[0]),
            "action": r[1],
            "user_id": str(r[2]) if r[2] is not None else "",
            "details": r[3] or {},
            "created_at": r[4],
        }
        for r in rows
    ]


def _audit_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Convert audit row dict to the API response shape."""
    details = row.get("details") or {}
    created_at = row.get("created_at")
    return {
        "id": row.get("id", ""),
        "action": row.get("action", ""),
        "user_id": row.get("user_id", ""),
        "user_name": details.get("actor") or row.get("user_id") or "",
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
        "details": details,
    }


@router.get("/audit")
def get_ai_settings_audit(
    user: dict[str, Any] = Depends(get_admin_principal_for_ai_settings),
    db: Session = Depends(get_db_for_ai_settings),
    limit: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    """Return recent AI settings audit events."""
    tid = _coerce_tenant_uuid(user)
    rows = _fetch_ai_audit_rows(db, tid, limit=limit)
    return {"items": [_audit_row_to_dict(r) for r in rows]}
