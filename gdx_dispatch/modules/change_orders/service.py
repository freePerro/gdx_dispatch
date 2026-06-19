from __future__ import annotations

import asyncio
import secrets
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event, utcnow
from gdx_dispatch.modules.change_orders.models import ModuleChangeOrder as ChangeOrder
from gdx_dispatch.modules.change_orders.models import ModuleChangeOrderLine as ChangeOrderLine


def _audit(db: Session, event_type: str, co_id: UUID, payload: dict) -> None:
    asyncio.run(log_audit_event(db, event_type, "system", "change_order", str(co_id), payload))


def _next_co_number(db: Session) -> str:
    seq = db.execute(select(ChangeOrder.co_number).order_by(ChangeOrder.created_at.desc(), ChangeOrder.id.desc()).limit(1)).scalar_one_or_none()
    next_n = int((seq or "CO-000").split("-")[-1]) + 1
    return f"CO-{next_n:03d}"


def create_change_order(job_id: UUID, title: str | None, description: str | None, lines: list[dict], db: Session) -> ChangeOrder:
    co = ChangeOrder(job_id=job_id, co_number=_next_co_number(db), title=title, description=description, customer_signature_token=secrets.token_urlsafe(32))
    db.add(co); db.flush(); total = 0.0  # noqa: E701,E702
    for line in lines:
        qty, unit_price = int(line.get("qty", 1)), float(line.get("unit_price", 0))
        line_total = qty * unit_price; total += line_total  # noqa: E701,E702
        db.add(ChangeOrderLine(co_id=co.id, description=line["description"], qty=qty, unit_price=unit_price, line_total=line_total))
    co.amount = total; _audit(db, "change_order_created", co.id, {"co_number": co.co_number, "amount": total}); db.commit(); db.refresh(co)  # noqa: E701,E702
    return co


def approve_change_order(co_id: UUID, approved_by: str, db: Session) -> ChangeOrder:
    co = db.execute(select(ChangeOrder).where(ChangeOrder.id == co_id)).scalar_one_or_none()
    if not co: raise HTTPException(status_code=404, detail="Change order not found")  # noqa: E701,E702
    co.status = "approved"; co.approved_at = utcnow(); co.approved_by = approved_by  # noqa: E701,E702
    _audit(db, "change_order_approved", co.id, {"approved_by": approved_by, "amount": float(co.amount or 0)}); db.commit(); db.refresh(co)  # noqa: E701,E702
    return co


def reject_change_order(co_id: UUID, db: Session) -> ChangeOrder:
    co = db.execute(select(ChangeOrder).where(ChangeOrder.id == co_id)).scalar_one_or_none()
    if not co: raise HTTPException(status_code=404, detail="Change order not found")  # noqa: E701,E702
    co.status = "rejected"; _audit(db, "change_order_rejected", co.id, {"co_number": co.co_number}); db.commit(); db.refresh(co)  # noqa: E701,E702
    return co
