"""Catalog policy API."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.settings_audit import audited_settings_upsert
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(prefix="/api/catalog-policy", tags=["catalog-policy"])


_COLS = (
    "catalog_require_description",
    "catalog_render_name_when_desc_empty",
    "catalog_ai_suggest_descriptions",
    "catalog_block_zero_price_on_invoice",
    "catalog_warn_zero_price_on_invoice",
    "catalog_block_zero_price_on_save",
    "catalog_auto_inactivate_zero_price",
)


class PolicyPayload(BaseModel):
    catalog_require_description: bool = False
    catalog_render_name_when_desc_empty: bool = True
    catalog_ai_suggest_descriptions: bool = False
    catalog_block_zero_price_on_invoice: bool = False
    catalog_warn_zero_price_on_invoice: bool = True
    catalog_block_zero_price_on_save: bool = False
    catalog_auto_inactivate_zero_price: bool = False


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
def get_policy_endpoint(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _ = user
    return _read(db, _tenant_uuid(request))


@router.patch("", response_model=None)
def update_policy(
    payload: PolicyPayload,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if (user.get("role") or "").lower() not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="admin or owner required")
    tid = _tenant_uuid(request)
    # Invariant #1: who changed which setting, to what, and when.
    return audited_settings_upsert(
        db,
        request,
        user,
        tenant_id=tid,
        values={c: getattr(payload, c) for c in _COLS},
        action="catalog_policy_updated",
        read=_read,
    )


# POST /suggest-description left 2026-09-10 (#637): nothing called it, it
# wrote nothing, and it sent a prompt to the AI on every request from any
# signed-in user.
