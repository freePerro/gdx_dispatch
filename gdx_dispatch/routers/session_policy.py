"""Session policy: tenant-wide inactivity auto-logout.

GET  /api/session-policy  — any signed-in user (the frontend reads this to
                            enforce the timeout for everyone in the tenant).
PATCH /api/session-policy — admin/owner only (sets the tenant-wide value).

Mirrors gdx_dispatch/modules/dispatch_settings/router.py (raw SQL on
tenant_settings, tenant from request context, INSERT-on-missing).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(prefix="/api/session-policy", tags=["session-policy"])

_COL = "session_idle_timeout_minutes"
_ADMIN_ROLES = {"admin", "owner", "superadmin"}


def _is_admin(role: str | None) -> bool:
    return (role or "").lower() in _ADMIN_ROLES


class SessionPolicyPayload(BaseModel):
    # 0 = disabled. Cap at 8h — beyond that it's effectively off and just risks
    # an int that overflows the frontend setTimeout (ms) range.
    idle_timeout_minutes: int = Field(default=0, ge=0, le=480)


def _tenant_uuid(request: Request) -> UUID:
    tid = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        return UUID(tid)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="invalid tenant context") from exc


def _read(db: Session, tid: UUID) -> dict[str, Any]:
    row = db.execute(
        text(f"SELECT {_COL} FROM tenant_settings WHERE tenant_id = :tid"),
        {"tid": str(tid)},
    ).first()
    if row is None:
        db.execute(
            text("INSERT INTO tenant_settings (tenant_id) VALUES (:tid) ON CONFLICT (tenant_id) DO NOTHING"),
            {"tid": str(tid)},
        )
        db.commit()
        row = db.execute(
            text(f"SELECT {_COL} FROM tenant_settings WHERE tenant_id = :tid"),
            {"tid": str(tid)},
        ).first()
    return {"idle_timeout_minutes": int(row[0]) if row and row[0] is not None else 0}


@router.get("", response_model=None)
def get_policy(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _ = user
    return _read(db, _tenant_uuid(request))


@router.patch("", response_model=None)
def update_policy(
    payload: SessionPolicyPayload,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not _is_admin(user.get("role")):
        raise HTTPException(status_code=403, detail="admin or owner required")
    tid = _tenant_uuid(request)
    db.execute(
        text(
            f"INSERT INTO tenant_settings (tenant_id, {_COL}) VALUES (:tid, :v) "
            f"ON CONFLICT (tenant_id) DO UPDATE SET {_COL} = :v"
        ),
        {"tid": str(tid), "v": payload.idle_timeout_minutes},
    )
    db.commit()
    return _read(db, tid)
