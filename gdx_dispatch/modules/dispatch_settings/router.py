"""Dispatch-settings API: GET (any signed-in user) + PATCH (admin/owner)."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(prefix="/api/dispatch-settings", tags=["dispatch-settings"])


_COLS = (
    "dispatch_warn_save_no_tech",
    "dispatch_block_save_no_tech",
    "dispatch_show_unassigned_lane",
)


class DispatchSettingsPayload(BaseModel):
    dispatch_warn_save_no_tech: bool = False
    dispatch_block_save_no_tech: bool = False
    # Default true — the lane is a safety net every dispatcher benefits from.
    dispatch_show_unassigned_lane: bool = True


def _tenant_uuid(request: Request) -> UUID:
    tid = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        return UUID(tid)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="invalid tenant context") from exc


def _read(db: Session, tid: UUID) -> dict[str, Any]:
    cols = ", ".join(_COLS)
    row = db.execute(
        text(f"SELECT {cols} FROM tenant_settings WHERE tenant_id = :tid"),
        {"tid": str(tid)},
    ).first()
    if row is None:
        db.execute(
            text("INSERT INTO tenant_settings (tenant_id) VALUES (:tid) ON CONFLICT (tenant_id) DO NOTHING"),
            {"tid": str(tid)},
        )
        db.commit()
        row = db.execute(
            text(f"SELECT {cols} FROM tenant_settings WHERE tenant_id = :tid"),
            {"tid": str(tid)},
        ).first()
    return {col: bool(row[i]) for i, col in enumerate(_COLS)}


@router.get("", response_model=None)
def get_settings_endpoint(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _ = user
    return _read(db, _tenant_uuid(request))


@router.patch("", response_model=None)
def update_settings(
    payload: DispatchSettingsPayload,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if (user.get("role") or "").lower() not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="admin or owner required")
    tid = _tenant_uuid(request)
    set_clause = ", ".join(f"{c} = :{c}" for c in _COLS)
    db.execute(
        text(
            f"INSERT INTO tenant_settings (tenant_id, {', '.join(_COLS)}) "
            f"VALUES (:tid, {', '.join(':' + c for c in _COLS)}) "
            f"ON CONFLICT (tenant_id) DO UPDATE SET {set_clause}"
        ),
        {"tid": str(tid), **{c: getattr(payload, c) for c in _COLS}},
    )
    db.commit()
    return _read(db, tid)
