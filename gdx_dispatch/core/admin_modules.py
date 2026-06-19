from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import TenantModuleGrant
from gdx_dispatch.core.admin_ops import _require_admin
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import MODULE_KEYS

router = APIRouter()


class ModuleGrantBody(BaseModel):
    module_key: str
    granted_by_tenant_id: UUID | None = None


@router.get("/tenants/{tenant_id}/modules", dependencies=[Depends(_require_admin)])
def list_tenant_modules(tenant_id: UUID, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    grants = db.query(TenantModuleGrant).filter_by(tenant_id=tenant_id).all()
    return [
        {
            "module_key": g.module_key,
            "granted_at": str(g.granted_at),
            "expires_at": str(g.expires_at) if g.expires_at else None,
            "granted_by_tenant_id": str(g.granted_by_tenant_id) if g.granted_by_tenant_id else None,
        }
        for g in grants
    ]


@router.post("/tenants/{tenant_id}/modules", dependencies=[Depends(_require_admin)])
def grant_module(tenant_id: UUID, body: ModuleGrantBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.module_key not in MODULE_KEYS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown module key: {body.module_key}")
    existing = db.query(TenantModuleGrant).filter_by(tenant_id=tenant_id, module_key=body.module_key).first()
    if existing:
        return {"status": "already_granted", "tenant_id": str(tenant_id), "module_key": body.module_key}
    grant = TenantModuleGrant(
        tenant_id=tenant_id,
        module_key=body.module_key,
        granted_by_tenant_id=body.granted_by_tenant_id,
    )
    db.add(grant)
    db.commit()
    db.refresh(grant)
    return {"status": "granted", "tenant_id": str(tenant_id), "module_key": grant.module_key}


@router.delete(
    "/tenants/{tenant_id}/modules/{module_key}",
    dependencies=[Depends(_require_admin)],
    status_code=status.HTTP_204_NO_CONTENT,
)
def revoke_module(tenant_id: UUID, module_key: str, db: Session = Depends(get_db)) -> None:
    grant = db.query(TenantModuleGrant).filter_by(tenant_id=tenant_id, module_key=module_key).first()
    if not grant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Module grant not found")
    db.delete(grant)
    db.commit()
