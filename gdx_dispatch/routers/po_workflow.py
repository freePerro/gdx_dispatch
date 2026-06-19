"""Purchase Order Workflow — PO requests linked to jobs with line items and receive flow."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
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
from gdx_dispatch.models.tenant_models import PORequest, PORequestLine, VanInventoryItem
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/purchase-orders",
    tags=["po-workflow"],
    dependencies=[Depends(require_module("inventory"))],
)

PO_STATUSES = ("requested", "approved", "ordered", "received", "cancelled")


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class POLineIn(BaseModel):
    sku: str | None = Field(default=None, max_length=100)
    name: str = Field(min_length=1, max_length=300)
    quantity: int = Field(default=1, ge=1, le=1_000_000)
    unit_price: float = Field(default=0, ge=0, le=1_000_000)


class POCreateIn(BaseModel):
    job_id: str | None = Field(default=None, max_length=36)
    customer_id: str | None = Field(default=None, max_length=36)
    supplier_name: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=5000)
    lines: list[POLineIn] = Field(default_factory=list, max_length=500)


class POPatchIn(BaseModel):
    status: str | None = Field(default=None, max_length=30)
    supplier_name: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=5000)


def _serialize_line(l: PORequestLine) -> dict[str, Any]:
    return {
        "id": str(l.id), "po_id": str(l.po_id),
        "sku": l.sku, "name": l.name,
        "quantity": int(l.quantity), "unit_price": float(l.unit_price or 0),
    }


def _serialize_po(po: PORequest, lines: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": str(po.id), "company_id": str(po.company_id),
        "requested_by": str(po.requested_by),
        "job_id": str(po.job_id) if po.job_id else None,
        "customer_id": str(po.customer_id) if po.customer_id else None,
        "supplier_name": po.supplier_name, "status": po.status, "notes": po.notes,
        "created_at": str(po.created_at) if po.created_at else None,
        "approved_at": str(po.approved_at) if po.approved_at else None,
        "received_at": str(po.received_at) if po.received_at else None,
        "lines": lines,
        "total": sum(l["quantity"] * l["unit_price"] for l in lines),
    }


def _fetch_po_with_lines(db: Session, po_id: str, tid: str) -> dict[str, Any]:
    po = db.execute(
        select(PORequest).where(PORequest.id == po_id, PORequest.company_id == tid, PORequest.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    lines = db.execute(select(PORequestLine).where(PORequestLine.po_id == po.id)).scalars().all()
    return _serialize_po(po, [_serialize_line(l) for l in lines])


@router.post("", status_code=201)
def create_po(
    request: Request,
    payload: POCreateIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    uid = _uid(user)
    now = _now()
    try:
        po = PORequest(
            id=uuid4(), company_id=tid, requested_by=uid,
            job_id=payload.job_id, customer_id=payload.customer_id,
            supplier_name=payload.supplier_name, status="requested",
            notes=payload.notes, created_at=now,
        )
        db.add(po)
        for line in payload.lines:
            db.add(PORequestLine(
                id=uuid4(), po_id=po.id, sku=line.sku, name=line.name,
                quantity=line.quantity, unit_price=Decimal(str(line.unit_price)),
            ))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("po_create_failed")
        raise HTTPException(status_code=500, detail="Failed to create purchase order") from None

    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="create",
        entity_type="po_request", entity_id=str(po.id),
        details={"job_id": payload.job_id, "supplier": payload.supplier_name, "line_count": len(payload.lines)},
        request=request,
    )
    return _fetch_po_with_lines(db, str(po.id), tid)


@router.get("")
def list_pos(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    status: str | None = Query(None),
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    q = select(PORequest).where(PORequest.deleted_at.is_(None))
    if status:
        if status not in PO_STATUSES:
            raise HTTPException(status_code=422, detail=f"Invalid status. Must be one of: {', '.join(PO_STATUSES)}")
        q = q.where(PORequest.status == status)
    q = q.order_by(PORequest.created_at.desc())
    pos = db.execute(q).scalars().all()

    result = []
    for po in pos:
        lines = db.execute(select(PORequestLine).where(PORequestLine.po_id == po.id)).scalars().all()
        result.append(_serialize_po(po, [_serialize_line(l) for l in lines]))
    return result


@router.patch("/{po_id}")
def update_po_status(
    po_id: str,
    request: Request,
    payload: POPatchIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    uid = _uid(user)

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    po = db.execute(
        select(PORequest).where(PORequest.id == po_id, PORequest.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    now = _now()
    if payload.status:
        if payload.status not in PO_STATUSES:
            raise HTTPException(status_code=422, detail=f"Invalid status. Must be one of: {', '.join(PO_STATUSES)}")
        po.status = payload.status
        if payload.status == "approved":
            po.approved_at = now
    if payload.supplier_name is not None:
        po.supplier_name = payload.supplier_name
    if payload.notes is not None:
        po.notes = payload.notes

    try:
        db.commit()
    except Exception:
        db.rollback()
        log.exception("po_update_failed")
        raise HTTPException(status_code=500, detail="Failed to update purchase order") from None

    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="update",
        entity_type="po_request", entity_id=po_id,
        details={"changes": payload.model_dump(exclude_none=True)},
        request=request,
    )
    return _fetch_po_with_lines(db, po_id, tid)


@router.post("/{po_id}/receive")
def receive_po(
    po_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    truck_id: str | None = Query(None, description="If set, add received items to van inventory for this truck"),
) -> dict[str, Any]:
    tid = _tid(request)
    uid = _uid(user)

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    po = db.execute(
        select(PORequest).where(PORequest.id == po_id, PORequest.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")

    now = _now()
    try:
        po.status = "received"
        po.received_at = now

        if truck_id:
            lines = db.execute(select(PORequestLine).where(PORequestLine.po_id == po.id)).scalars().all()
            for line in lines:
                db.add(VanInventoryItem(
                    id=uuid4(), company_id=tid, truck_id=truck_id,
                    sku=line.sku, name=line.name, quantity=int(line.quantity),
                    min_stock=0, category=None, created_at=now, updated_at=now,
                ))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("po_receive_failed")
        raise HTTPException(status_code=500, detail="Failed to receive purchase order") from None

    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="update",
        entity_type="po_request", entity_id=po_id,
        details={"action": "receive", "truck_id": truck_id},
        request=request,
    )
    return _fetch_po_with_lines(db, po_id, tid)
