from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.inventory.models import JobPart, Part
from gdx_dispatch.modules.inventory.service import deduct_stock

# GET/POST /inventory/parts and GET /inventory/low-stock left this module 2026-09-06
# (#569): routers/inventory.py registers them first, so FastAPI never dispatched here.
# The four routes below are unique to this module.
router = APIRouter(prefix="/api", tags=["inventory"], dependencies=[Depends(require_module("inventory"))])

class PartPatch(BaseModel): sku: str | None = None; name: str | None = None; description: str | None = None; unit_cost: float | None = None; unit_price: float | None = None; qty_on_hand: int | None = None; reorder_point: int | None = None; vendor_name: str | None = None; vendor_sku: str | None = None  # noqa: E701,E702
class StockAdjust(BaseModel): delta: int  # noqa: E701,E702
class JobPartIn(BaseModel): part_id: UUID; qty_used: int = 1  # noqa: E701,E702


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
