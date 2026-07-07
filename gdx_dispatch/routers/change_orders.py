"""
Change Orders router — mid-job scope/price changes.

Port of archive/dispatch_flask/blueprints/api_change_orders.py (13 endpoints).
Tracks scope changes to jobs with customer approval workflow.
"""
from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, Uuid, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from gdx_dispatch.core.audit import TenantBase, log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["change_orders"],
    dependencies=[Depends(require_module("change_orders"))],
)


CO_STATUSES = ("draft", "pending_approval", "approved", "declined", "completed")


class ChangeOrder(TenantBase):
    __tablename__ = "change_orders"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    co_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=True)
    customer_name: Mapped[str] = mapped_column(String(200), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(String(120), nullable=True)  # customer_request, scope_added, etc.
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    approved_by: Mapped[str] = mapped_column(String(200), nullable=True)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    signature_url: Mapped[str] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    customer_signature_token: Mapped[str] = mapped_column(String(255), nullable=True)
    # PR3-billing-capture (S122 pattern, mirrors job_parts_needed): the
    # invoice this CO was billed on. Set by the invoice-create handler via
    # UPDATE…RETURNING — the stamp GATES the line copy, so a CO can never
    # bill twice. NULL = never billed. FK ON DELETE SET NULL releases COs
    # when an invoice is hard-deleted (soft-delete handler does the same).
    # Billing truth lives HERE — `status` stays approval-workflow truth
    # (billing a CO does not flip its status).
    billed_invoice_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("invoices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class ChangeOrderLineIn(BaseModel):
    """D-S122-change-orders-create-flow — line-item shape for the create
    payload. Mirrors the InvoiceLineCreateIn shape so the frontend can mount
    the same <LineItemEditor> component."""
    description: str = Field(min_length=1, max_length=500)
    quantity: int = Field(default=1, gt=0, le=9999)
    unit_price: float = Field(default=0, ge=0, le=999999.99)
    # PR3-billing-capture: COs are handled like invoices (Doug 2026-07-07) —
    # default True mirrors InvoiceLineCreateIn; labor lines send False where
    # the state allows.
    taxable: bool = Field(default=True)


class ChangeOrderIn(BaseModel):
    job_id: str | None = Field(default=None, max_length=36)
    customer_id: str | None = Field(default=None, max_length=36)
    customer_name: str | None = Field(default=None, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    reason: str | None = Field(default=None, max_length=1000)
    # $1M ceiling catches obvious nonsense; ge=0 rejects negative amounts.
    # When `line_items` is supplied, this is recomputed from the line subtotal.
    # None-default so PATCH can distinguish "not sent" (preserve) from
    # "set to 0" (legacy bare-amount caller). Auditor round-2 catch.
    amount: float | None = Field(default=None, ge=0, le=1_000_000)
    status: str = Field(default="draft", min_length=1, max_length=50)
    # D-S122-change-orders-create-flow — optional line-items list. When set,
    # ChangeOrderLine rows are created and `amount` is overwritten by the sum.
    line_items: list[ChangeOrderLineIn] = Field(default_factory=list)


def _serialize(
    co: ChangeOrder,
    lines: list[Any] | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(co.id),
        "co_number": co.co_number,
        "job_id": str(co.job_id) if co.job_id else None,
        "customer_id": str(co.customer_id) if co.customer_id else None,
        "customer_name": co.customer_name,
        "title": co.title,
        "description": co.description,
        "reason": co.reason,
        "status": co.status,
        "amount": float(co.amount or 0),
        "billed_invoice_id": str(co.billed_invoice_id) if co.billed_invoice_id else None,
        "approved_by": co.approved_by,
        "approved_at": co.approved_at.isoformat() if co.approved_at else None,
        "signature_url": co.signature_url,
        "created_by": co.created_by,
        "created_at": co.created_at.isoformat() if co.created_at else None,
        "updated_at": co.updated_at.isoformat() if co.updated_at else None,
    }
    if lines is not None:
        out["line_items"] = [
            {
                "id": str(ln.id),
                "description": ln.description,
                "quantity": int(ln.qty or 1),
                "unit_price": float(ln.unit_price or 0),
                "line_total": float(ln.line_total or 0),
                "taxable": bool(getattr(ln, "taxable", True)),
            }
            for ln in lines
        ]
        # PR3-billing-capture (Doug 2026-07-07): "handled like an invoice —
        # tax shown so the customer sees it." Same resolver as invoices, so
        # the total the customer signs equals the total the invoice bills.
        # Tax failure degrades to rate 0 with a LOUD log, never a 500 — the
        # approval screen must render even if tax config is broken.
        subtotal = sum(float(ln.line_total or 0) for ln in lines)
        tax_rate = 0.0
        if db is not None:
            try:
                from gdx_dispatch.modules.tax.service import resolve_rate
                tax_rate = float(resolve_rate(db, co.customer_id))
            except Exception:
                log.exception("change_order_tax_resolve_failed co=%s", co.id)
        taxable_subtotal = sum(
            float(ln.line_total or 0)
            for ln in lines
            if bool(getattr(ln, "taxable", True))
        )
        tax_amount = round(taxable_subtotal * tax_rate, 2)
        out["subtotal"] = round(subtotal, 2)
        out["tax_rate"] = tax_rate
        out["tax_amount"] = tax_amount
        out["total"] = round(subtotal + tax_amount, 2)
    return out


def _next_co_number(db: Session) -> str:
    count = db.execute(select(ChangeOrder)).scalars().all()
    return f"CO-{len(count) + 1:05d}"


@router.get("/api/change-orders", response_model=None)
def list_change_orders(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    job_id: str | None = None,
    status: str | None = None,
    unbilled: bool = False,
) -> list[dict[str, Any]]:
    stmt = select(ChangeOrder).where(ChangeOrder.deleted_at.is_(None))
    if job_id:
        with contextlib.suppress(ValueError):
            stmt = stmt.where(ChangeOrder.job_id == UUID(job_id))
    if status:
        stmt = stmt.where(ChangeOrder.status == status)
    if unbilled:
        # PR3-billing-capture: the invoice-create checklist — approved,
        # never billed. Mirrors GET parts-needed?unbilled=true (S122).
        stmt = stmt.where(
            ChangeOrder.status == "approved",
            ChangeOrder.billed_invoice_id.is_(None),
        )
    rows = db.execute(stmt.order_by(ChangeOrder.created_at.desc())).scalars().all()
    return [_serialize(r) for r in rows]


@router.post("/api/change-orders", response_model=None, status_code=201)
def create_change_order(
    payload: ChangeOrderIn,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not payload.title.strip():
        raise HTTPException(status_code=422, detail="Title is required")
    # D-S122-change-orders-create-flow — when line_items are provided, sum
    # them and override the flat amount field. Operators can still send a
    # bare amount (legacy callers + edits-without-lines).
    computed_amount = (
        sum(float(li.unit_price) * int(li.quantity) for li in payload.line_items)
        if payload.line_items else float(payload.amount or 0)
    )
    co = ChangeOrder(
        co_number=_next_co_number(db),
        job_id=UUID(payload.job_id) if payload.job_id else None,
        customer_id=UUID(payload.customer_id) if payload.customer_id else None,
        customer_name=payload.customer_name,
        title=payload.title,
        description=payload.description,
        reason=payload.reason,
        status=payload.status if payload.status in CO_STATUSES else "draft",
        amount=Decimal(str(computed_amount)),
        created_by=user.get("email") if isinstance(user, dict) else None,
    )
    db.add(co)
    db.flush()
    # Insert line items in the same transaction. ChangeOrderLine is the
    # canonical store; the flat `amount` on ChangeOrder is the denormalized
    # subtotal for fast list rendering.
    if payload.line_items:
        from gdx_dispatch.models.tenant_models import ChangeOrderLine
        for li in payload.line_items:
            db.add(ChangeOrderLine(
                co_id=co.id,
                description=li.description,
                qty=li.quantity,
                unit_price=Decimal(str(li.unit_price)),
                line_total=Decimal(str(float(li.unit_price) * int(li.quantity))),
                taxable=bool(li.taxable),
            ))
    db.commit()
    db.refresh(co)
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
                action="create_change_order",
                entity_type="change_order",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('create_change_order_audit_failed')
    return _serialize(co)


@router.get("/api/change-orders/{co_id}", response_model=None)
def get_change_order(co_id: UUID, _: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    co = db.get(ChangeOrder, co_id)
    if not co or co.deleted_at:
        raise HTTPException(status_code=404, detail="Change order not found")
    # D-S122-change-orders-create-flow — return line_items on detail view.
    # PR3-billing-capture: pass db so the detail/approval view carries
    # subtotal/tax/total (COs are handled like invoices).
    from gdx_dispatch.models.tenant_models import ChangeOrderLine
    lines = db.execute(
        select(ChangeOrderLine).where(ChangeOrderLine.co_id == co.id)
    ).scalars().all()
    return _serialize(co, lines=lines, db=db)


@router.patch("/api/change-orders/{co_id}", response_model=None)
def update_change_order(
    co_id: UUID,
    payload: ChangeOrderIn,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    co = db.get(ChangeOrder, co_id)
    if not co or co.deleted_at:
        raise HTTPException(status_code=404, detail="Change order not found")
    # PR3 audit round 2: a billed CO is frozen — editing lines/amount after
    # billing silently diverges the invoice from the signed CO, and the
    # delta can never bill (the stamp holds). Release it first by deleting
    # the draft invoice that owns it.
    if co.billed_invoice_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{co.co_number} is billed on an invoice — delete that draft invoice to release it before editing.",
        )
    for field in ("title", "description", "reason", "customer_name"):
        val = getattr(payload, field, None)
        if val is not None:
            setattr(co, field, val)
    # D-S122-change-orders-create-flow auditor catch: PATCH was silently
    # dropping line_items, so the frontend's edit-with-lines flow blanked
    # the line set + set amount=0. Now we replace ChangeOrderLine rows in
    # the same transaction: delete the old set, write the new one, and
    # recompute amount from the new subtotal.
    from gdx_dispatch.models.tenant_models import ChangeOrderLine
    if payload.line_items:
        # Replace strategy: delete existing rows, insert the new set. Safe
        # because ChangeOrderLine has no FK references TO it (consumer is
        # only ChangeOrder itself).
        db.execute(
            ChangeOrderLine.__table__.delete().where(ChangeOrderLine.co_id == co.id)
        )
        for li in payload.line_items:
            db.add(ChangeOrderLine(
                co_id=co.id,
                description=li.description,
                qty=li.quantity,
                unit_price=Decimal(str(li.unit_price)),
                line_total=Decimal(str(float(li.unit_price) * int(li.quantity))),
                taxable=bool(li.taxable),
            ))
        co.amount = Decimal(str(
            sum(float(li.unit_price) * int(li.quantity) for li in payload.line_items)
        ))
    elif payload.amount is not None:
        co.amount = Decimal(str(payload.amount))
    if payload.status in CO_STATUSES:
        co.status = payload.status
    db.commit()
    db.refresh(co)
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
                action="update_change_order",
                entity_type="change_order",
                entity_id=str(co_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('update_change_order_audit_failed')
    return _serialize(co)


@router.post("/api/change-orders/{co_id}/approve", response_model=None)
def approve_change_order(
    co_id: UUID,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    co = db.get(ChangeOrder, co_id)
    if not co or co.deleted_at:
        raise HTTPException(status_code=404, detail="Change order not found")
    co.status = "approved"
    co.approved_by = user.get("email") if isinstance(user, dict) else "system"
    co.approved_at = utcnow()
    db.commit()
    db.refresh(co)
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
                action="approve_change_order",
                entity_type="change_order",
                entity_id=str(co_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('approve_change_order_audit_failed')
    return _serialize(co)


@router.post("/api/change-orders/{co_id}/decline", response_model=None)
def decline_change_order(co_id: UUID, _: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    co = db.get(ChangeOrder, co_id)
    if not co or co.deleted_at:
        raise HTTPException(status_code=404, detail="Change order not found")
    co.status = "declined"
    db.commit()
    db.refresh(co)
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
                action="decline_change_order",
                entity_type="change_order",
                entity_id=str(co_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('decline_change_order_audit_failed')
    return _serialize(co)


@router.delete("/api/change-orders/{co_id}", response_model=None, status_code=204)
def delete_change_order(co_id: UUID, _: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    co = db.get(ChangeOrder, co_id)
    if not co or co.deleted_at:
        raise HTTPException(status_code=404, detail="Change order not found")
    # PR3 audit round 2: same freeze as PATCH — deleting a billed CO would
    # orphan the invoice lines' provenance.
    if co.billed_invoice_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{co.co_number} is billed on an invoice — delete that draft invoice to release it first.",
        )
    co.deleted_at = utcnow()
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
                action="delete_change_order",
                entity_type="change_order",
                entity_id=str(co_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('delete_change_order_audit_failed')
    return None
