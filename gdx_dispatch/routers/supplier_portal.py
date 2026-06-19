"""Supplier Portal Bridge — connect dealers to distributor catalogs and ordering."""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import SupplierCatalogItem, SupplierOrder, SupplierOrderLine
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/supplier",
    tags=["supplier-portal"],
    dependencies=[Depends(require_module("inventory"))],
)


class CatalogItemCreate(BaseModel):
    supplier_name: str = Field(min_length=1, max_length=200)
    sku: str | None = Field(default=None, max_length=100)
    name: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    unit_price: float = Field(ge=0)
    stock_level: int = Field(default=0, ge=0)
    category: str | None = Field(default=None, max_length=100)


class OrderLineRequest(BaseModel):
    sku: str | None = None
    name: str = Field(min_length=1, max_length=300)
    quantity: int = Field(ge=1, le=10000)
    unit_price: float = Field(ge=0)


class CreateOrderRequest(BaseModel):
    supplier_name: str = Field(min_length=1, max_length=200)
    items: list[OrderLineRequest] = Field(min_length=1)
    notes: str | None = Field(default=None, max_length=2000)


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _serialize_catalog(item: SupplierCatalogItem) -> dict[str, Any]:
    return {
        "id": str(item.id), "company_id": str(item.company_id),
        "supplier_name": item.supplier_name, "sku": item.sku, "name": item.name,
        "description": item.description, "unit_price": float(item.unit_price or 0),
        "stock_level": int(item.stock_level or 0), "category": item.category,
        "created_at": str(item.created_at) if item.created_at else None,
    }


def _serialize_order(order: SupplierOrder) -> dict[str, Any]:
    return {
        "id": str(order.id), "company_id": str(order.company_id),
        "supplier_name": order.supplier_name, "status": order.status,
        "total_amount": float(order.total_amount or 0), "notes": order.notes,
        "created_at": str(order.created_at) if order.created_at else None,
        "updated_at": str(order.updated_at) if order.updated_at else None,
    }


@router.get("/catalog")
def list_catalog(request: Request, supplier_name: str | None = Query(default=None),
                 category: str | None = Query(default=None),
                 user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    q = select(SupplierCatalogItem)
    if supplier_name:
        q = q.where(SupplierCatalogItem.supplier_name == supplier_name)
    if category:
        q = q.where(SupplierCatalogItem.category == category)
    q = q.order_by(SupplierCatalogItem.supplier_name, SupplierCatalogItem.name)
    return {"items": [_serialize_catalog(item) for item in db.execute(q).scalars().all()]}


@router.post("/catalog", status_code=201)
def add_catalog_item(body: CatalogItemCreate, request: Request,
                     user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    tid = _tid(request)
    item = SupplierCatalogItem(
        id=uuid4(), company_id=tid, supplier_name=body.supplier_name, sku=body.sku,
        name=body.name, description=body.description,
        unit_price=Decimal(str(body.unit_price)), stock_level=body.stock_level, category=body.category,
    )
    db.add(item)
    db.commit()
    log_audit_event_sync(db, tenant_id=tid, user_id=str(user.get("sub", "system")),
        action="supplier_catalog_item_added", entity_type="supplier_catalog", entity_id=str(item.id),
        details={"name": body.name, "supplier": body.supplier_name}, request=request)
    db.commit()
    return {"id": str(item.id), "name": body.name}


@router.post("/orders", status_code=201)
def create_order(body: CreateOrderRequest, request: Request,
                 user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    tid = _tid(request)
    total = sum(item.quantity * item.unit_price for item in body.items)
    order = SupplierOrder(
        id=uuid4(), company_id=tid, supplier_name=body.supplier_name,
        status="pending", total_amount=Decimal(str(total)), notes=body.notes,
    )
    db.add(order)
    for item in body.items:
        lt = item.quantity * item.unit_price
        db.add(SupplierOrderLine(
            id=uuid4(), order_id=order.id, sku=item.sku, name=item.name,
            quantity=item.quantity, unit_price=Decimal(str(item.unit_price)),
            line_total=Decimal(str(lt)),
        ))
    db.commit()
    log_audit_event_sync(db, tenant_id=tid, user_id=str(user.get("sub", "system")),
        action="supplier_order_created", entity_type="supplier_order", entity_id=str(order.id),
        details={"supplier": body.supplier_name, "total": total, "items": len(body.items)}, request=request)
    db.commit()
    return {"id": str(order.id), "supplier_name": body.supplier_name, "status": "pending", "total_amount": total}


@router.get("/orders")
def list_orders(request: Request, supplier_name: str | None = Query(default=None),
                status: str | None = Query(default=None),
                user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    q = select(SupplierOrder)
    if supplier_name:
        q = q.where(SupplierOrder.supplier_name == supplier_name)
    if status:
        q = q.where(SupplierOrder.status == status)
    q = q.order_by(SupplierOrder.created_at.desc())
    return {"items": [_serialize_order(o) for o in db.execute(q).scalars().all()]}


@router.get("/orders/{order_id}")
def get_order(order_id: str, request: Request,
              user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    order = db.execute(
        select(SupplierOrder).where(SupplierOrder.id == order_id)
    ).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    lines = db.execute(select(SupplierOrderLine).where(SupplierOrderLine.order_id == order.id)).scalars().all()
    result = _serialize_order(order)
    result["items"] = [
        {"id": str(l.id), "sku": l.sku, "name": l.name, "quantity": int(l.quantity),
         "unit_price": float(l.unit_price or 0), "line_total": float(l.line_total or 0)}
        for l in lines
    ]
    return result
