from __future__ import annotations

import asyncio
import random
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event, utcnow
from gdx_dispatch.modules.inventory.models import Part
from gdx_dispatch.modules.purchase_orders.models import InventoryPurchaseOrder as PurchaseOrder
from gdx_dispatch.modules.purchase_orders.models import InventoryPurchaseOrderLine as PurchaseOrderLine


def _audit(db: Session, event_type: str, po_id: UUID, payload: dict) -> None:
    asyncio.run(log_audit_event(db, event_type, "system", "purchase_order", str(po_id), payload))


def create_po(vendor_name: str, job_id: UUID | None, lines: list[dict], db: Session) -> PurchaseOrder:
    po = PurchaseOrder(po_number=f"PO-{datetime.now().strftime('%Y%m%d')}-{random.randint(1000, 9999)}", vendor_name=vendor_name, job_id=job_id)
    db.add(po); db.flush()  # noqa: E701,E702
    total = 0.0
    for line in lines:
        qty, unit_cost = int(line.get("qty", 1)), float(line.get("unit_cost", 0))
        line_total = qty * unit_cost; total += line_total  # noqa: E701,E702
        db.add(PurchaseOrderLine(po_id=po.id, part_id=line.get("part_id"), description=line["description"], qty=qty, unit_cost=unit_cost, line_total=line_total))
    po.total_amount = total; po.idempotency_key = f"po-{po.id}"; _audit(db, "po_created", po.id, {"po_number": po.po_number, "total_amount": total}); db.commit(); db.refresh(po)  # noqa: E701,E702
    return po


def receive_po(po_id: UUID, db: Session) -> PurchaseOrder:
    po = db.execute(select(PurchaseOrder).where(PurchaseOrder.id == po_id, PurchaseOrder.deleted_at.is_(None)).with_for_update()).scalar_one_or_none()
    if not po: raise HTTPException(status_code=404, detail="PO not found")  # noqa: E701,E702
    lines = list(db.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id)).scalars().all())
    for line in lines:
        if not line.part_id: continue  # noqa: E701,E702
        part = db.execute(select(Part).where(Part.id == line.part_id, Part.deleted_at.is_(None)).with_for_update()).scalar_one_or_none()
        if part: part.qty_on_hand += line.qty  # noqa: E701,E702
    po.status = "received"; po.received_at = utcnow(); _audit(db, "po_received", po.id, {"line_count": len(lines)}); db.commit(); db.refresh(po)  # noqa: E701,E702
    return po
