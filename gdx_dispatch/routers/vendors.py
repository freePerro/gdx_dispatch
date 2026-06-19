"""
Vendors router — supplier master (CHI, Clopay, Amarr, Wayne Dalton, etc.)

Port of archive/dispatch_flask/blueprints/api_vendors.py endpoints.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(tags=["vendors"], dependencies=[Depends(require_module("inventory"))])


from gdx_dispatch.models.tenant_models import Vendor  # noqa: E402

log = logging.getLogger(__name__)


class VendorIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    account_number: str | None = Field(default=None, max_length=100)
    contact_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=254)
    website: str | None = Field(default=None, max_length=500)
    address: str | None = Field(default=None, max_length=500)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=50)
    zip: str | None = Field(default=None, max_length=20)
    notes: str | None = Field(default=None, max_length=5000)
    payment_terms: str | None = Field(default=None, max_length=100)
    tax_id: str | None = Field(default=None, max_length=50)
    active: bool = True


def _serialize(v: Vendor) -> dict[str, Any]:
    return {
        "id": str(v.id),
        "name": v.name,
        "account_number": v.account_number,
        "contact_name": v.contact_name,
        "phone": v.phone,
        "email": v.email,
        "website": v.website,
        "address": v.address,
        "city": v.city,
        "state": v.state,
        "zip": v.zip,
        "notes": v.notes,
        "payment_terms": v.payment_terms,
        "tax_id": v.tax_id,
        "active": v.active,
        "qb_vendor_id": v.qb_vendor_id,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "updated_at": v.updated_at.isoformat() if v.updated_at else None,
    }


@router.get("/api/vendors", response_model=None)
def list_vendors(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    search: str | None = None,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    stmt = select(Vendor).where(Vendor.deleted_at.is_(None))
    if active_only:
        stmt = stmt.where(Vendor.active.is_(True))
    if search:
        q = f"%{search.lower()}%"
        stmt = stmt.where(or_(
            Vendor.name.ilike(q), Vendor.contact_name.ilike(q), Vendor.email.ilike(q),
        ))
    rows = db.execute(stmt.order_by(Vendor.name)).scalars().all()
    return [_serialize(r) for r in rows]


@router.post("/api/vendors", response_model=None, status_code=201)
def create_vendor(
    payload: VendorIn,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="Vendor name is required")
    v = Vendor(**payload.model_dump())
    db.add(v)
    db.commit()
    db.refresh(v)
    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="create_vendor",
                entity_type="vendor",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('create_vendor_audit_failed')
    return _serialize(v)


@router.get("/api/vendors/{vendor_id}", response_model=None)
def get_vendor(
    vendor_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    v = db.get(Vendor, vendor_id)
    if not v or v.deleted_at:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return _serialize(v)


@router.patch("/api/vendors/{vendor_id}", response_model=None)
def update_vendor(
    vendor_id: UUID,
    payload: VendorIn,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    v = db.get(Vendor, vendor_id)
    if not v or v.deleted_at:
        raise HTTPException(status_code=404, detail="Vendor not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(v, field, value)
    db.commit()
    db.refresh(v)
    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="update_vendor",
                entity_type="vendor",
                entity_id=str(vendor_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('update_vendor_audit_failed')
    return _serialize(v)


@router.delete("/api/vendors/{vendor_id}", response_model=None, status_code=204)
def delete_vendor(
    vendor_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    v = db.get(Vendor, vendor_id)
    if not v or v.deleted_at:
        raise HTTPException(status_code=404, detail="Vendor not found")
    v.deleted_at = utcnow()
    db.commit()
    # TODO(audit): verify action/entity_type/entity_id/details for this handler
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="delete_vendor",
                entity_type="vendor",
                entity_id=str(vendor_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('delete_vendor_audit_failed')
    return None
