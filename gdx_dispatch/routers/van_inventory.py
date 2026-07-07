"""Van Inventory — per-truck parts inventory with usage logging."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import VanInventoryItem, VanInventoryLog
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/van-inventory",
    tags=["van-inventory"],
    dependencies=[Depends(require_module("inventory"))],
)


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class VanItemIn(BaseModel):
    truck_id: str = Field(min_length=1, max_length=36)
    sku: str | None = Field(default=None, max_length=100)
    name: str = Field(min_length=1, max_length=300)
    quantity: int = Field(default=0, ge=0, le=10_000_000)
    min_stock: int = Field(default=0, ge=0, le=10_000_000)
    category: str | None = Field(default=None, max_length=100)


class VanUseIn(BaseModel):
    van_inventory_id: str = Field(min_length=1, max_length=64)
    job_id: str | None = Field(default=None, max_length=36)
    quantity: int = Field(gt=0, le=10_000_000)
    reason: str = Field(default="used_on_job", max_length=500)


def _serialize(item: VanInventoryItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "company_id": str(item.company_id),
        "truck_id": str(item.truck_id),
        "sku": item.sku,
        "name": item.name,
        "quantity": int(item.quantity),
        "min_stock": int(item.min_stock),
        "category": item.category,
        "created_at": str(item.created_at) if item.created_at else None,
        "updated_at": str(item.updated_at) if item.updated_at else None,
    }


@router.get("")
def list_van_items(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    truck_id: str | None = Query(None),
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    q = select(VanInventoryItem).where(
        VanInventoryItem.deleted_at.is_(None),
    )
    if truck_id:
        q = q.where(VanInventoryItem.truck_id == truck_id).order_by(VanInventoryItem.name)
    else:
        q = q.order_by(VanInventoryItem.truck_id, VanInventoryItem.name)
    return [_serialize(item) for item in db.execute(q).scalars().all()]


@router.post("", status_code=201)
def add_van_item(
    request: Request,
    payload: VanItemIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    uid = _uid(user)
    now = _now()
    try:
        item = VanInventoryItem(
            id=uuid4(), company_id=tid, truck_id=payload.truck_id,
            sku=payload.sku, name=payload.name, quantity=payload.quantity,
            min_stock=payload.min_stock, category=payload.category,
            created_at=now, updated_at=now,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
    except Exception:
        db.rollback()
        log.exception("van_inventory_create_failed")
        raise HTTPException(status_code=500, detail="Failed to add van inventory item") from None

    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="create",
        entity_type="van_inventory", entity_id=str(item.id),
        details={"truck_id": payload.truck_id, "name": payload.name, "quantity": payload.quantity},
        request=request,
    )
    return _serialize(item)


@router.post("/use")
def use_van_item(
    request: Request,
    payload: VanUseIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    uid = _uid(user)

    # PR4: bind a UUID object — the Uuid column rejects a raw str on the
    # SQLite test path; malformed ids 404 instead of 500.
    from uuid import UUID as _UUID
    try:
        _item_uuid = _UUID(str(payload.van_inventory_id))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Van inventory item not found") from None

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    item = db.execute(
        select(VanInventoryItem).where(
            VanInventoryItem.id == _item_uuid,
            VanInventoryItem.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Van inventory item not found")

    if payload.quantity > item.quantity:
        raise HTTPException(status_code=422, detail=f"Insufficient stock: {item.quantity} available, {payload.quantity} requested")

    # PR4 audit round 2: job_id was free-form — a typo minted a checklist row
    # no query could ever display (an invisible unbillable row, the exact
    # leak this PR closes). Validate BEFORE any mutation and FAIL LOUDLY on
    # garbage instead of storing it. (Validated up here, outside the generic
    # try/except below, so it surfaces as 422 — not a swallowed 500.)
    _job_key: str | None = None
    if payload.job_id:
        try:
            _job_key = str(_UUID(str(payload.job_id)))
        except (ValueError, AttributeError):
            raise HTTPException(status_code=422, detail=f"invalid job_id: {payload.job_id}") from None

    now = _now()
    item.quantity -= payload.quantity
    item.updated_at = now
    try:
        log_entry = VanInventoryLog(
            id=uuid4(), van_inventory_id=item.id,
            job_id=payload.job_id, quantity_change=-payload.quantity,
            reason=payload.reason, created_by=uid, created_at=now,
        )
        db.add(log_entry)
        # PR4-billing-capture: van usage decremented truck stock but the part
        # NEVER reached billing. When the usage is job-linked, add one
        # source-tagged billable checklist row per event (events accumulate).
        # Van items carry no sell price — the office prices it at invoicing.
        if _job_key:
            from gdx_dispatch.models.tenant_models import JobPartNeeded
            db.add(JobPartNeeded(
                id=str(uuid4()),
                company_id=tid,
                job_id=_job_key,
                part_name=item.name,
                sku=item.sku,
                quantity=int(payload.quantity),
                status="used",
                source="van",
                notes=(f"van stock ({payload.reason})" if payload.reason else "van stock"),
                requested_by_user_id=uid,
                created_at=now,
                updated_at=now,
            ))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("van_inventory_use_failed")
        raise HTTPException(status_code=500, detail="Failed to deduct van inventory") from None

    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="update",
        entity_type="van_inventory", entity_id=str(item.id),
        details={"action": "use", "quantity_deducted": payload.quantity, "job_id": payload.job_id, "new_quantity": item.quantity},
        request=request,
    )
    return {"id": str(item.id), "new_quantity": item.quantity, "log_id": str(log_entry.id)}


@router.get("/low-stock")
def low_stock_items(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    items = db.execute(
        select(VanInventoryItem).where(
            VanInventoryItem.deleted_at.is_(None),
            VanInventoryItem.min_stock > 0,
            VanInventoryItem.quantity < VanInventoryItem.min_stock,
        ).order_by((VanInventoryItem.min_stock - VanInventoryItem.quantity).desc())
    ).scalars().all()
    return [_serialize(item) for item in items]
