from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.inventory.models import JobPart, Part
from gdx_dispatch.modules.inventory.service import check_low_stock_alerts, deduct_stock
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(prefix="/api", tags=["inventory"], dependencies=[Depends(require_module("inventory"))])

class PartIn(BaseModel): sku: str; name: str; description: str | None = None; unit_cost: float = 0; unit_price: float = 0; qty_on_hand: int = 0; reorder_point: int = 0; vendor_name: str | None = None; vendor_sku: str | None = None  # noqa: E701,E702
class PartPatch(BaseModel): sku: str | None = None; name: str | None = None; description: str | None = None; unit_cost: float | None = None; unit_price: float | None = None; qty_on_hand: int | None = None; reorder_point: int | None = None; vendor_name: str | None = None; vendor_sku: str | None = None  # noqa: E701,E702
class StockAdjust(BaseModel): delta: int  # noqa: E701,E702
class JobPartIn(BaseModel): part_id: UUID; qty_used: int = 1  # noqa: E701,E702

@router.get("/inventory/parts", response_model=None)
def list_parts(search: str | None = None, db: Session = Depends(get_db)) -> list[Part]:
    q = select(Part).where(Part.deleted_at.is_(None))
    if search: q = q.where(or_(Part.sku.ilike(f"%{search}%"), Part.name.ilike(f"%{search}%")))  # noqa: E701,E702
    return list(db.execute(q.order_by(Part.created_at.desc())).scalars().all())

@router.post("/inventory/parts", response_model=None)
def create_part(payload: PartIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> Part:
    if user.get("role") not in {"owner", "admin"}: raise HTTPException(status_code=403, detail="Insufficient role")  # noqa: E701,E702
    part = Part(**payload.model_dump()); db.add(part); db.commit(); db.refresh(part); return part  # noqa: E701,E702

@router.put("/inventory/parts/{part_id}", response_model=None)
def update_part(part_id: UUID, payload: PartPatch, db: Session = Depends(get_db)) -> Part:
    part = db.execute(select(Part).where(Part.id == part_id, Part.deleted_at.is_(None))).scalar_one_or_none()
    if not part: raise HTTPException(status_code=404, detail="Part not found")  # noqa: E701,E702
    for k, v in payload.model_dump(exclude_unset=True).items(): setattr(part, k, v)  # noqa: E701,E702
    db.commit(); db.refresh(part); return part  # noqa: E701,E702

@router.get("/inventory/parts/{part_id}/stock", response_model=None)
def get_stock(part_id: UUID, db: Session = Depends(get_db)) -> dict[str, object]:
    part = db.execute(select(Part).where(Part.id == part_id, Part.deleted_at.is_(None))).scalar_one_or_none()
    if not part: raise HTTPException(status_code=404, detail="Part not found")  # noqa: E701,E702
    return {"qty_on_hand": part.qty_on_hand, "reorder_point": part.reorder_point, "low_stock": part.qty_on_hand <= part.reorder_point}

@router.post("/inventory/parts/{part_id}/adjust", response_model=None)
def adjust_stock(part_id: UUID, payload: StockAdjust, db: Session = Depends(get_db)) -> Part:
    part = db.execute(select(Part).where(Part.id == part_id, Part.deleted_at.is_(None)).with_for_update()).scalar_one_or_none()
    if not part: raise HTTPException(status_code=404, detail="Part not found")  # noqa: E701,E702
    part.qty_on_hand += payload.delta; db.commit(); db.refresh(part); return part  # noqa: E701,E702

@router.post("/jobs/{job_id}/parts", response_model=None)
def attach_part(job_id: UUID, payload: JobPartIn, db: Session = Depends(get_db)) -> JobPart:
    part = deduct_stock(payload.part_id, payload.qty_used, db)
    row = JobPart(job_id=job_id, part_id=part.id, qty_used=payload.qty_used, unit_cost_at_time=part.unit_cost)
    db.add(row); db.commit(); db.refresh(row); return row  # noqa: E701,E702

@router.get("/inventory/low-stock", response_model=None)
def low_stock(db: Session = Depends(get_db)) -> list[Part]:
    return check_low_stock_alerts(db)
