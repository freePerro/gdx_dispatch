from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.inventory.models import Part


def deduct_stock(part_id: UUID, qty: int, db: Session) -> Part:
    if qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be > 0")
    part = db.execute(select(Part).where(Part.id == part_id, Part.deleted_at.is_(None)).with_for_update()).scalar_one_or_none()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    if part.qty_on_hand < qty:
        raise HTTPException(status_code=400, detail="Insufficient stock")
    part.qty_on_hand -= qty
    return part


def check_low_stock_alerts(db: Session) -> list[Part]:
    return list(db.execute(select(Part).where(Part.deleted_at.is_(None), Part.qty_on_hand <= Part.reorder_point)).scalars().all())
