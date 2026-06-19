"""
Inventory router — parts, stock, adjustments.

Matches the endpoints Vue InventoryView.vue expects at /api/inventory/parts.
Also supports /api/inventory/items (the porting target from Flask api_inventory.py).
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import InventoryItem, StockAdjustment
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(tags=["inventory"], dependencies=[Depends(require_module("inventory"))])


class InventoryItemIn(BaseModel):
    part_name: str | None = Field(default=None, max_length=200)
    name: str | None = Field(default=None, max_length=200)  # alias for part_name
    sku: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    quantity: int = Field(default=0, ge=0, le=10_000_000)
    reorder_level: int = Field(default=5, ge=0, le=10_000_000)
    unit_cost: float = Field(default=0, ge=0, le=1_000_000)
    unit_price: float = Field(default=0, ge=0, le=1_000_000)
    supplier: str | None = Field(default=None, max_length=200)
    vendor_id: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=200)
    manufacturer_part_number: str | None = Field(default=None, max_length=120)


class StockAdjustmentIn(BaseModel):
    quantity_delta: int = Field(..., ge=-10_000_000, le=10_000_000, description="Positive to add, negative to remove")
    reason: str = Field(default="manual", max_length=64)
    notes: str | None = Field(default=None, max_length=2000)
    job_id: str | None = Field(default=None, max_length=64)


def _serialize(item: InventoryItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "part_name": item.part_name,
        "name": item.part_name,  # alias for Vue
        "sku": item.sku,
        "description": item.description,
        "quantity": item.quantity,
        "quantity_on_hand": item.quantity,  # alias
        "reorder_level": item.reorder_level,
        "reorder_point": item.reorder_level,  # alias
        "unit_cost": float(item.unit_cost or 0),
        "unit_price": float(item.unit_price or 0),
        "supplier": item.supplier,
        "vendor_id": item.vendor_id,
        "category": item.category,
        "location": item.location,
        "manufacturer_part_number": item.manufacturer_part_number,
        "is_low_stock": item.quantity <= item.reorder_level and item.reorder_level > 0,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


# ── Parts endpoints (Vue InventoryView) ──

@router.get("/api/inventory/parts", response_model=None)
def list_parts(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    search: str | None = Query(None),
) -> list[dict[str, Any]]:
    stmt = select(InventoryItem).where(InventoryItem.deleted_at.is_(None))
    if search:
        q = f"%{search.lower()}%"
        stmt = stmt.where(or_(
            InventoryItem.part_name.ilike(q),
            InventoryItem.sku.ilike(q),
            InventoryItem.supplier.ilike(q),
        ))
    rows = db.execute(stmt.order_by(InventoryItem.part_name)).scalars().all()
    return [_serialize(r) for r in rows]


@router.post("/api/inventory/parts", response_model=None, status_code=201)
def create_part(
    payload: InventoryItemIn,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    name = payload.part_name or payload.name
    if not name:
        raise HTTPException(status_code=422, detail="part_name is required")
    item = InventoryItem(
        part_name=name,
        sku=payload.sku,
        description=payload.description,
        quantity=payload.quantity,
        reorder_level=payload.reorder_level,
        unit_cost=Decimal(str(payload.unit_cost or 0)),
        unit_price=Decimal(str(payload.unit_price or 0)),
        supplier=payload.supplier,
        vendor_id=payload.vendor_id,
        category=payload.category,
        location=payload.location,
        manufacturer_part_number=payload.manufacturer_part_number,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
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
                action="create_part",
                entity_type="part",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('create_part_audit_failed')
    return _serialize(item)


@router.get("/api/inventory/parts/{item_id}", response_model=None)
def get_part(
    item_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = db.get(InventoryItem, item_id)
    if not item or item.deleted_at:
        raise HTTPException(status_code=404, detail="Part not found")
    return _serialize(item)


@router.patch("/api/inventory/parts/{item_id}", response_model=None)
def update_part(
    item_id: UUID,
    payload: InventoryItemIn,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = db.get(InventoryItem, item_id)
    if not item or item.deleted_at:
        raise HTTPException(status_code=404, detail="Part not found")

    for field in ("sku", "description", "quantity", "reorder_level", "supplier",
                   "vendor_id", "category", "location", "manufacturer_part_number"):
        val = getattr(payload, field, None)
        if val is not None:
            setattr(item, field, val)
    if payload.part_name or payload.name:
        item.part_name = payload.part_name or payload.name
    if payload.unit_cost is not None:
        item.unit_cost = Decimal(str(payload.unit_cost))
    if payload.unit_price is not None:
        item.unit_price = Decimal(str(payload.unit_price))

    db.commit()
    db.refresh(item)
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
                action="update_part",
                entity_type="part",
                entity_id=str(item_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('update_part_audit_failed')
    return _serialize(item)


@router.delete("/api/inventory/parts/{item_id}", response_model=None, status_code=204)
def delete_part(
    item_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.get(InventoryItem, item_id)
    if not item or item.deleted_at:
        raise HTTPException(status_code=404, detail="Part not found")
    item.deleted_at = utcnow()
    db.commit()
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
                action="delete_part",
                entity_type="part",
                entity_id=str(item_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('delete_part_audit_failed')
    return None


# ── Alias endpoints for /api/inventory/items (Flask port) ──

@router.get("/api/inventory/items", response_model=None)
def list_items_alias(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    search: str | None = Query(None),
) -> list[dict[str, Any]]:
    return list_parts(_=_, db=db, search=search)


@router.post("/api/inventory/items/{item_id}/adjust", response_model=None)
def adjust_stock(
    item_id: UUID,
    payload: StockAdjustmentIn,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = db.get(InventoryItem, item_id)
    if not item or item.deleted_at:
        raise HTTPException(status_code=404, detail="Part not found")

    item.quantity = max(0, item.quantity + payload.quantity_delta)

    adjustment = StockAdjustment(
        item_id=item_id,
        quantity_delta=payload.quantity_delta,
        reason=payload.reason,
        notes=payload.notes,
        job_id=UUID(payload.job_id) if payload.job_id else None,
    )
    db.add(adjustment)
    db.commit()
    db.refresh(item)
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
                action="adjust_stock",
                entity_type="inventory_item",
                entity_id=str(item_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('adjust_stock_audit_failed')
    return {"item": _serialize(item), "new_quantity": item.quantity}


@router.get("/api/inventory/low-stock", response_model=None)
def list_low_stock(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(InventoryItem).where(
        InventoryItem.deleted_at.is_(None),
        InventoryItem.quantity <= InventoryItem.reorder_level,
        InventoryItem.reorder_level > 0,
    )
    rows = db.execute(stmt).scalars().all()
    return [_serialize(r) for r in rows]
