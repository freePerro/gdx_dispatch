from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.change_orders.models import ModuleChangeOrder as ChangeOrder
from gdx_dispatch.modules.change_orders.models import ModuleChangeOrderLine as ChangeOrderLine
from gdx_dispatch.modules.change_orders.service import approve_change_order, create_change_order, reject_change_order

router = APIRouter(prefix="/api", tags=["change_orders"])

class COLineIn(BaseModel): description: str; qty: int = 1; unit_price: float = 0  # noqa: E701,E702
class COCreateIn(BaseModel): title: str | None = None; description: str | None = None; lines: list[COLineIn] = Field(default_factory=list)  # noqa: E701,E702
class COApproveIn(BaseModel): approved_by: str = "system"  # noqa: E701,E702

@router.get("/jobs/{job_id}/change-orders", response_model=None)
def list_change_orders(job_id: UUID, _: None = Depends(require_module("change_orders")), db: Session = Depends(get_db)) -> list[ChangeOrder]:
    return list(db.execute(select(ChangeOrder).where(ChangeOrder.job_id == job_id).order_by(ChangeOrder.created_at.desc())).scalars().all())

@router.post("/jobs/{job_id}/change-orders", response_model=None)
def post_change_order(job_id: UUID, payload: COCreateIn, _: None = Depends(require_module("change_orders")), db: Session = Depends(get_db)) -> ChangeOrder:
    return create_change_order(job_id, payload.title, payload.description, [l.model_dump() for l in payload.lines], db)  # noqa: E741

@router.get("/change-orders/{co_id}", response_model=None)
def get_change_order(co_id: UUID, _: None = Depends(require_module("change_orders")), db: Session = Depends(get_db)) -> dict[str, object]:
    co = db.execute(select(ChangeOrder).where(ChangeOrder.id == co_id)).scalar_one_or_none()
    if not co: raise HTTPException(status_code=404, detail="Change order not found")  # noqa: E701,E702
    return {"change_order": co, "lines": list(db.execute(select(ChangeOrderLine).where(ChangeOrderLine.co_id == co.id)).scalars().all())}

@router.post("/change-orders/{co_id}/approve", response_model=None)
def post_approve_change_order(co_id: UUID, payload: COApproveIn, _: None = Depends(require_module("change_orders")), db: Session = Depends(get_db)) -> ChangeOrder:
    return approve_change_order(co_id, payload.approved_by, db)

@router.post("/change-orders/{co_id}/reject", response_model=None)
def post_reject_change_order(co_id: UUID, _: None = Depends(require_module("change_orders")), db: Session = Depends(get_db)) -> ChangeOrder:
    return reject_change_order(co_id, db)

@router.get("/change-orders/approve/{token}")
def customer_approve_change_order(token: str, db: Session = Depends(get_db)) -> dict[str, str]:
    co = db.execute(select(ChangeOrder).where(ChangeOrder.customer_signature_token == token)).scalar_one_or_none()
    if not co: raise HTTPException(status_code=404, detail="Invalid approval token")  # noqa: E701,E702
    approve_change_order(co.id, "customer", db); return {"status": "approved", "co_id": str(co.id), "note": "Invoice should be created for this amount."}  # noqa: E701,E702
