from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.purchase_orders.models import InventoryPurchaseOrder as PurchaseOrder
from gdx_dispatch.modules.purchase_orders.models import InventoryPurchaseOrderLine as PurchaseOrderLine
from gdx_dispatch.modules.purchase_orders.service import create_po, receive_po

router = APIRouter(prefix="/api", tags=["purchase_orders"], dependencies=[Depends(require_module("purchase_orders"))])

class POLineIn(BaseModel): part_id: UUID | None = None; description: str; qty: int = 1; unit_cost: float = 0  # noqa: E701,E702
class POCreateIn(BaseModel): vendor_name: str; vendor_email: str | None = None; job_id: UUID | None = None; notes: str | None = None; lines: list[POLineIn] = Field(default_factory=list)  # noqa: E701,E702

@router.get("/purchase-orders", response_model=None)
def list_purchase_orders(status: str | None = None, db: Session = Depends(get_db)) -> list[PurchaseOrder]:
    q = select(PurchaseOrder).where(PurchaseOrder.deleted_at.is_(None))
    if status: q = q.where(PurchaseOrder.status == status)  # noqa: E701,E702
    return list(db.execute(q.order_by(PurchaseOrder.created_at.desc())).scalars().all())

@router.post("/purchase-orders", response_model=None)
def post_purchase_order(payload: POCreateIn, db: Session = Depends(get_db)) -> PurchaseOrder:
    po = create_po(payload.vendor_name, payload.job_id, [l.model_dump() for l in payload.lines], db)  # noqa: E741
    po.vendor_email = payload.vendor_email; po.notes = payload.notes; db.commit(); db.refresh(po); return po  # noqa: E701,E702

@router.get("/purchase-orders/{po_id}", response_model=None)
def get_purchase_order(po_id: UUID, db: Session = Depends(get_db)) -> dict[str, object]:
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.id == po_id, PurchaseOrder.deleted_at.is_(None))).scalar_one_or_none()
    if not po: raise HTTPException(status_code=404, detail="PO not found")  # noqa: E701,E702
    lines = list(db.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id)).scalars().all()); return {"po": po, "lines": lines}  # noqa: E701,E702

@router.post("/purchase-orders/{po_id}/send", response_model=None)
def send_purchase_order(po_id: UUID, db: Session = Depends(get_db)) -> PurchaseOrder:
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.id == po_id, PurchaseOrder.deleted_at.is_(None))).scalar_one_or_none()
    if not po: raise HTTPException(status_code=404, detail="PO not found")  # noqa: E701,E702
    po.status = "sent"; po.sent_at = utcnow(); asyncio.run(log_audit_event(db, "po_sent", "system", "purchase_order", str(po.id), {"po_number": po.po_number})); db.commit(); db.refresh(po); return po  # noqa: E701,E702

@router.post("/purchase-orders/{po_id}/receive", response_model=None)
def post_receive_purchase_order(po_id: UUID, db: Session = Depends(get_db)) -> PurchaseOrder:
    return receive_po(po_id, db)
