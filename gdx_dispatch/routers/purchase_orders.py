"""
Purchase Orders router — vendor POs with line items + receive workflow.

Port of archive/dispatch_flask/blueprints/api_purchase_orders.py + api_inventory.py PO subset.
"""
from __future__ import annotations

import contextlib
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, Uuid, select
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from gdx_dispatch.core.audit import TenantBase, log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.door_specs import door_specs_for_job, receiving_view
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import InventoryItem
from gdx_dispatch.modules.inventory.stock import apply_stock_delta
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(tags=["purchase_orders"], dependencies=[Depends(require_module("inventory"))])


PO_STATUSES = ("draft", "sent", "received", "cancelled")


class PurchaseOrder(TenantBase):
    __tablename__ = "purchase_orders"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    po_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    vendor_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    vendor_name: Mapped[str] = mapped_column(String(200), nullable=True)  # denormalized
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")
    order_date: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    expected_date: Mapped[date] = mapped_column(Date, nullable=True)
    received_date: Mapped[date] = mapped_column(Date, nullable=True)
    # The job this PO was ordered for (nullable — vendor-stock POs have none).
    # Lets receiving follow the thread to the job's captured door specs: what
    # should arrive + how heavy. Added by migration 031; create_all builds it on
    # a fresh DB. See gdx_dispatch/core/door_specs.py.
    job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    # The door receiving specs FROZEN at the moment this PO was linked to a job —
    # {doors: [receiving_view...], estimate_number, job_number}. Receiving must
    # validate against what THIS PO ordered, not whatever the job's latest quote
    # says now: revising an accepted estimate after the PO is cut must not move
    # the target. Nullable — set only for job-linked POs; migration 032.
    door_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
    subtotal: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    tax: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    shipping: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    created_by: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    lines: Mapped[list[PurchaseOrderLine]] = relationship(
        back_populates="po", cascade="all, delete-orphan"
    )


class PurchaseOrderLine(TenantBase):
    __tablename__ = "purchase_order_lines"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    po_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("purchase_orders.id"), nullable=False, index=True)
    item_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True)  # FK to inventory_items
    sku: Mapped[str] = mapped_column(String(100), nullable=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    quantity_ordered: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    quantity_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unit_cost: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    line_total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)

    po: Mapped[PurchaseOrder] = relationship(back_populates="lines")


class POLineIn(BaseModel):
    item_id: str | None = Field(default=None, max_length=64)
    sku: str | None = Field(default=None, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    quantity_ordered: int = Field(default=1, ge=0, le=1_000_000)
    unit_cost: float = Field(default=0, ge=0, le=1_000_000)


class PurchaseOrderIn(BaseModel):
    vendor_id: str | None = Field(default=None, max_length=64)
    vendor_name: str | None = Field(default=None, max_length=200)
    job_id: str | None = Field(default=None, max_length=64)  # PO ordered for this job
    status: str = Field(default="draft", max_length=50)
    order_date: date | None = None
    expected_date: date | None = None
    notes: str | None = Field(default=None, max_length=5000)
    tax: float = Field(default=0, ge=0, le=1_000_000)
    shipping: float = Field(default=0, ge=0, le=1_000_000)
    lines: list[POLineIn] = Field(default_factory=list, max_length=500)


def _serialize_line(line: PurchaseOrderLine) -> dict[str, Any]:
    return {
        "id": str(line.id),
        "po_id": str(line.po_id),
        "item_id": str(line.item_id) if line.item_id else None,
        "sku": line.sku,
        "description": line.description,
        "quantity_ordered": line.quantity_ordered,
        "quantity_received": line.quantity_received,
        "unit_cost": float(line.unit_cost or 0),
        "line_total": float(line.line_total or 0),
    }


def _serialize_po(po: PurchaseOrder) -> dict[str, Any]:
    return {
        "id": str(po.id),
        "po_number": po.po_number,
        "vendor_id": str(po.vendor_id) if po.vendor_id else None,
        "vendor_name": po.vendor_name,
        "job_id": str(po.job_id) if po.job_id else None,
        "status": po.status,
        "order_date": po.order_date.isoformat() if po.order_date else None,
        "expected_date": po.expected_date.isoformat() if po.expected_date else None,
        "received_date": po.received_date.isoformat() if po.received_date else None,
        "notes": po.notes,
        "subtotal": float(po.subtotal or 0),
        "tax": float(po.tax or 0),
        "shipping": float(po.shipping or 0),
        "total": float(po.total or 0),
        "created_by": po.created_by,
        "created_at": po.created_at.isoformat() if po.created_at else None,
        "lines": [_serialize_line(l) for l in po.lines],
    }


def _resolve_receiving(db: Session, job_id) -> dict[str, Any]:
    """Live-compute a job's receiving snapshot: its captured door(s) as a
    receiving view + the numbers it's known by. This is what gets FROZEN onto the
    PO at link time (via door_snapshot) and the fallback for POs that predate it.
    Shape: {doors: [...], estimate_number, job_number}."""
    if not job_id:
        return {"doors": [], "job_number": None, "estimate_number": None}
    from gdx_dispatch.models.tenant_models import Job
    from gdx_dispatch.modules.proposals.models import Estimate

    doors = [receiving_view(d) for d in door_specs_for_job(db, job_id)]
    job = db.get(Job, job_id)
    est = db.execute(
        select(Estimate)
        .where(Estimate.job_id == job_id, Estimate.deleted_at.is_(None))
        .order_by(Estimate.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return {
        "doors": doors,
        "job_number": getattr(job, "job_number", None) if job else None,
        "estimate_number": getattr(est, "estimate_number", None) if est else None,
    }


def _po_receiving(db: Session, po: PurchaseOrder) -> dict[str, Any]:
    """The receiving info returned with a PO. Prefer the snapshot frozen at
    link time — what THIS PO actually ordered, immune to later quote revisions.
    Fall back to a live resolve only for POs created before door_snapshot
    existed. Detail/receive surfaces only — kept off the list to avoid a
    per-row query fan-out."""
    # A frozen snapshot WITH doors is authoritative — that's what the PO ordered.
    # An empty/missing snapshot (PO linked before the door was captured, or a PO
    # predating door_snapshot) falls back to a live resolve so a later capture
    # still surfaces.
    snap = po.door_snapshot
    if not isinstance(snap, dict) or not snap.get("doors"):
        snap = _resolve_receiving(db, po.job_id)
    return {
        "door_specs": snap.get("doors", []),
        "job_number": snap.get("job_number"),
        "estimate_number": snap.get("estimate_number"),
    }


def _calculate_totals(po: PurchaseOrder) -> None:
    subtotal = sum(float(l.unit_cost or 0) * int(l.quantity_ordered or 0) for l in po.lines)
    po.subtotal = Decimal(str(subtotal))
    po.total = Decimal(str(subtotal + float(po.tax or 0) + float(po.shipping or 0)))
    for line in po.lines:
        line.line_total = Decimal(str(float(line.unit_cost or 0) * int(line.quantity_ordered or 0)))


def _next_po_number(db: Session) -> str:
    """Generate next PO number like PO-000123."""
    count = db.execute(select(PurchaseOrder)).scalars().all()
    return f"PO-{len(count) + 1:06d}"


@router.get("/api/purchase-orders", response_model=None)
def list_pos(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    status: str | None = None,
    vendor_id: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(PurchaseOrder).where(PurchaseOrder.deleted_at.is_(None))
    if status:
        stmt = stmt.where(PurchaseOrder.status == status)
    if vendor_id:
        with contextlib.suppress(ValueError):
            stmt = stmt.where(PurchaseOrder.vendor_id == UUID(vendor_id))
    rows = db.execute(stmt.order_by(PurchaseOrder.created_at.desc())).scalars().all()
    return [_serialize_po(p) for p in rows]


@router.post("/api/purchase-orders", response_model=None, status_code=201)
def create_po(
    payload: PurchaseOrderIn,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _job_uuid = None
    if payload.job_id:
        with contextlib.suppress(ValueError):
            _job_uuid = UUID(payload.job_id)
    po = PurchaseOrder(
        po_number=_next_po_number(db),
        vendor_id=UUID(payload.vendor_id) if payload.vendor_id else None,
        vendor_name=payload.vendor_name,
        job_id=_job_uuid,
        # Freeze the door receiving specs at PO-creation time — what THIS PO
        # ordered, immune to later quote revisions.
        door_snapshot=_resolve_receiving(db, _job_uuid) if _job_uuid else None,
        status=payload.status if payload.status in PO_STATUSES else "draft",
        order_date=payload.order_date or date.today(),
        expected_date=payload.expected_date,
        notes=payload.notes,
        tax=Decimal(str(payload.tax)),
        shipping=Decimal(str(payload.shipping)),
        created_by=user.get("email") if isinstance(user, dict) else None,
    )
    for line_in in payload.lines:
        line = PurchaseOrderLine(
            item_id=UUID(line_in.item_id) if line_in.item_id else None,
            sku=line_in.sku,
            description=line_in.description,
            quantity_ordered=line_in.quantity_ordered,
            unit_cost=Decimal(str(line_in.unit_cost)),
        )
        po.lines.append(line)
    _calculate_totals(po)
    db.add(po)
    db.commit()
    db.refresh(po)
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
                action="create_po",
                entity_type="po",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('create_po_audit_failed')
    return _serialize_po(po)


@router.get("/api/purchase-orders/{po_id}", response_model=None)
def get_po(
    po_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    po = db.get(PurchaseOrder, po_id)
    if not po or po.deleted_at:
        raise HTTPException(status_code=404, detail="PO not found")
    return {**_serialize_po(po), **_po_receiving(db, po)}


@router.patch("/api/purchase-orders/{po_id}", response_model=None)
def update_po(
    po_id: UUID,
    payload: PurchaseOrderIn,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    po = db.get(PurchaseOrder, po_id)
    if not po or po.deleted_at:
        raise HTTPException(status_code=404, detail="PO not found")
    if po.status in ("received", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Cannot edit {po.status} PO")

    for field in ("vendor_name", "notes", "expected_date"):
        val = getattr(payload, field, None)
        if val is not None:
            setattr(po, field, val)
    if payload.vendor_id:
        with contextlib.suppress(ValueError):
            po.vendor_id = UUID(payload.vendor_id)
    # Always set from the payload (unlike vendor_id) so the form's "clear" —
    # which PATCHes job_id: null — actually unlinks the job. An invalid uuid is
    # ignored rather than clearing, so a malformed value can't silently unlink.
    _old_job_id = po.job_id
    if payload.job_id:
        with contextlib.suppress(ValueError):
            po.job_id = UUID(payload.job_id)
    else:
        po.job_id = None
    # Only (re)freeze the door snapshot when the job link actually CHANGED. A
    # plain edit — draft→sent, a note, a line tweak — must NOT re-absorb a quote
    # that was revised since the PO was cut; that would silently defeat the whole
    # point of the freeze. Re-link → re-freeze against the new job; clear → drop.
    if po.job_id != _old_job_id:
        po.door_snapshot = _resolve_receiving(db, po.job_id) if po.job_id else None
    if payload.status in PO_STATUSES:
        po.status = payload.status
    po.tax = Decimal(str(payload.tax or 0))
    po.shipping = Decimal(str(payload.shipping or 0))

    # Replace lines if provided
    if payload.lines is not None:
        po.lines.clear()
        for line_in in payload.lines:
            po.lines.append(PurchaseOrderLine(
                item_id=UUID(line_in.item_id) if line_in.item_id else None,
                sku=line_in.sku,
                description=line_in.description,
                quantity_ordered=line_in.quantity_ordered,
                unit_cost=Decimal(str(line_in.unit_cost)),
            ))

    _calculate_totals(po)
    db.commit()
    db.refresh(po)
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
                action="update_po",
                entity_type="po",
                entity_id=str(po_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('update_po_audit_failed')
    return {**_serialize_po(po), **_po_receiving(db, po)}


@router.post("/api/purchase-orders/{po_id}/receive", response_model=None)
def receive_po(
    po_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Mark PO as received and increment inventory stock."""
    po = db.get(PurchaseOrder, po_id)
    if not po or po.deleted_at:
        raise HTTPException(status_code=404, detail="PO not found")
    if po.status == "received":
        raise HTTPException(status_code=409, detail="PO already received")

    for line in po.lines:
        if line.item_id:
            item = db.get(InventoryItem, line.item_id)
            if item:
                # Atomic delta (locks the item row) so two PO receives — or a PO
                # receive racing a vendor-invoice stock confirm — can't lost-update.
                apply_stock_delta(
                    db,
                    item,
                    delta=line.quantity_ordered,
                    reason="po_receive",
                    notes=f"Received from PO {po.po_number}",
                )
        line.quantity_received = line.quantity_ordered

    po.status = "received"
    po.received_date = date.today()
    db.commit()
    db.refresh(po)
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
                action="receive_po",
                entity_type="receive_po",
                entity_id=str(po_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('receive_po_audit_failed')
    return {**_serialize_po(po), **_po_receiving(db, po)}


@router.delete("/api/purchase-orders/{po_id}", response_model=None, status_code=204)
def delete_po(
    po_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    po = db.get(PurchaseOrder, po_id)
    if not po or po.deleted_at:
        raise HTTPException(status_code=404, detail="PO not found")
    if po.status == "received":
        raise HTTPException(status_code=409, detail="Cannot delete received PO")
    po.deleted_at = utcnow()
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
                action="delete_po",
                entity_type="po",
                entity_id=str(po_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('delete_po_audit_failed')
    return None
