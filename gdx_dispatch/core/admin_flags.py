from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import PlatformFeatureFlag as FeatureFlag
from gdx_dispatch.core.admin_ops import _require_admin
from gdx_dispatch.core.database import get_db

router = APIRouter()


class FlagCreate(BaseModel):
    flag_key: str
    rollout_pct: int = Field(default=0, ge=0, le=100)
    tenant_overrides: dict[str, bool] = Field(default_factory=dict)


class FlagUpdate(BaseModel):
    rollout_pct: int | None = Field(default=None, ge=0, le=100)
    tenant_overrides: dict[str, bool] | None = None


@router.get("/flags", dependencies=[Depends(_require_admin)])
def list_flags(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    flags = db.query(FeatureFlag).order_by(FeatureFlag.flag_key).all()
    return [
        {
            "flag_key": f.flag_key,
            "rollout_pct": f.rollout_pct,
            "tenant_overrides": f.tenant_overrides or {},
            "created_at": str(f.created_at),
        }
        for f in flags
    ]


@router.post("/flags", dependencies=[Depends(_require_admin)], status_code=status.HTTP_201_CREATED)
def create_flag(body: FlagCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    flag = db.query(FeatureFlag).filter_by(flag_key=body.flag_key).first()
    if flag:
        flag.rollout_pct = body.rollout_pct
        flag.tenant_overrides = body.tenant_overrides
    else:
        flag = FeatureFlag(
            flag_key=body.flag_key,
            rollout_pct=body.rollout_pct,
            tenant_overrides=body.tenant_overrides,
        )
        db.add(flag)
    db.commit()
    db.refresh(flag)
    return {"status": "created", "flag_key": flag.flag_key}


@router.patch("/flags/{flag_key}", dependencies=[Depends(_require_admin)])
def update_flag(flag_key: str, body: FlagUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    flag = db.query(FeatureFlag).filter_by(flag_key=flag_key).first()
    if not flag:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flag not found")
    if body.rollout_pct is None and body.tenant_overrides is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")
    if body.rollout_pct is not None:
        flag.rollout_pct = body.rollout_pct
    if body.tenant_overrides is not None:
        flag.tenant_overrides = body.tenant_overrides
    db.commit()
    return {"status": "updated", "flag_key": flag_key}


@router.get("/flags/{flag_key}/check/{tenant_id}", dependencies=[Depends(_require_admin)])
def check_flag(flag_key: str, tenant_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    from gdx_dispatch.core.feature_flags import is_flag_enabled
    enabled = is_flag_enabled(flag_key, tenant_id, db)
    return {"flag_key": flag_key, "tenant_id": tenant_id, "enabled": enabled}
