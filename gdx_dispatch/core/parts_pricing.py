"""gdx_dispatch/core/parts_pricing.py — Parts pricing intelligence.

Manages part cost/sell prices per tenant with margin tracking,
bulk updates, and markup suggestions based on tenant's own margin history.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import JSON, Column, DateTime, Numeric, String, asc, desc
from sqlalchemy.orm import Session
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_role

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------

class PartPrice(TenantBase):
    __tablename__ = "part_prices"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(String(100), index=True, nullable=False)
    part_number = Column(String(100), index=True, nullable=False)
    part_name = Column(String(200), nullable=False)
    cost_price = Column(Numeric(12, 2), nullable=False, default=0)
    sell_price = Column(Numeric(12, 2), nullable=False, default=0)
    margin_pct = Column(Numeric(6, 4), nullable=False, default=0)
    supplier = Column(String(200), nullable=True)
    last_updated_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    price_history = Column(JSON, nullable=True, default=list)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _compute_margin(cost: float, sell: float) -> float:
    """Return gross margin as a fraction: (sell - cost) / sell."""
    if sell <= 0:
        return 0.0
    return (sell - cost) / sell


def _part_to_dict(p: PartPrice, include_history: bool = False) -> dict:
    d = {
        "id": str(p.id),
        "tenant_id": p.tenant_id,
        "part_number": p.part_number,
        "part_name": p.part_name,
        "cost_price": float(p.cost_price),
        "sell_price": float(p.sell_price),
        "margin_pct": float(p.margin_pct),
        "supplier": p.supplier,
        "last_updated_at": p.last_updated_at.isoformat() if p.last_updated_at else None,
    }
    if include_history:
        d["price_history"] = p.price_history or []
    return d


def _history_entry(cost: float, sell: float, margin: float) -> dict:
    return {
        "date": datetime.now(timezone.utc).isoformat(),
        "cost_price": cost,
        "sell_price": sell,
        "margin_pct": margin,
    }


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class PartPriceCreate(BaseModel):
    tenant_id: str
    part_number: str
    part_name: str
    cost_price: float
    sell_price: float
    supplier: str | None = None


class PartPriceBulkItem(BaseModel):
    part_number: str
    part_name: str
    cost_price: float
    sell_price: float
    supplier: str | None = None


class PartPriceBulkUpdate(BaseModel):
    items: list[PartPriceBulkItem]


class SuggestMarkupRequest(BaseModel):
    cost_price: float


# ---------------------------------------------------------------------------
# FastAPI router
# NOTE: Specific routes (/margin-analysis, /bulk-update, /suggest-markup)
#       must be registered BEFORE the parameterized /{part_number} route.
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/parts", tags=["parts-pricing"])

_auth_dep = Depends(require_role("admin", "owner", "tech"))


@router.get("/pricing/margin-analysis")
def margin_analysis(
    request: Request,
    db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> dict:
    """Return all parts sorted by margin DESC with a summary."""
    tenant = getattr(request.state, "tenant", None)
    if not tenant:
        raise HTTPException(status_code=400, detail="Tenant context missing")
    tenant_id = str(tenant["id"])

    parts = (
        db.query(PartPrice)
        .filter(PartPrice.tenant_id == tenant_id, PartPrice.deleted_at.is_(None))
        .order_by(desc(PartPrice.margin_pct))
        .all()
    )

    rows = [
        {
            "part_number": p.part_number,
            "part_name": p.part_name,
            "cost_price": float(p.cost_price),
            "sell_price": float(p.sell_price),
            "margin_pct": float(p.margin_pct),
            "supplier": p.supplier,
        }
        for p in parts
    ]

    avg_margin = 0.0
    highest = ""
    lowest = ""
    if rows:
        avg_margin = sum(r["margin_pct"] for r in rows) / len(rows)
        highest = rows[0]["part_name"]
        lowest = rows[-1]["part_name"]

    return {
        "parts": rows,
        "summary": {
            "avg_margin": round(avg_margin, 4),
            "highest_margin_part": highest,
            "lowest_margin_part": lowest,
        },
    }


@router.post("/pricing/bulk-update")
def bulk_update(
    body: PartPriceBulkUpdate,
    request: Request,
    db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> dict:
    """Upsert multiple parts from a list. Returns counts of created/updated."""
    tenant = getattr(request.state, "tenant", None)
    if not tenant:
        raise HTTPException(status_code=400, detail="Tenant context missing")
    tenant_id = str(tenant["id"])

    updated = 0
    created = 0
    now = datetime.now(timezone.utc)

    for item in body.items:
        margin = _compute_margin(item.cost_price, item.sell_price)
        existing = (
            db.query(PartPrice)
            .filter(
                PartPrice.tenant_id == tenant_id,
                PartPrice.part_number == item.part_number,
                PartPrice.deleted_at.is_(None),
            )
            .first()
        )
        if existing:
            history = list(existing.price_history or [])
            history.append(_history_entry(float(existing.cost_price), float(existing.sell_price), float(existing.margin_pct)))
            existing.part_name = item.part_name
            existing.cost_price = item.cost_price
            existing.sell_price = item.sell_price
            existing.margin_pct = margin
            existing.supplier = item.supplier
            existing.last_updated_at = now
            existing.price_history = history
            updated += 1
        else:
            new_part = PartPrice(
                tenant_id=tenant_id,
                part_number=item.part_number,
                part_name=item.part_name,
                cost_price=item.cost_price,
                sell_price=item.sell_price,
                margin_pct=margin,
                supplier=item.supplier,
                last_updated_at=now,
                price_history=[_history_entry(item.cost_price, item.sell_price, margin)],
                created_at=now,
            )
            db.add(new_part)
            created += 1

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Bulk update failed: {exc}") from exc

    return {"updated": updated, "created": created}


@router.post("/pricing/suggest-markup")
def suggest_markup(
    body: SuggestMarkupRequest,
    request: Request,
    db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> dict:
    """Suggest a sell price based on this tenant's average margin."""
    tenant = getattr(request.state, "tenant", None)
    if not tenant:
        raise HTTPException(status_code=400, detail="Tenant context missing")
    tenant_id = str(tenant["id"])

    parts = (
        db.query(PartPrice)
        .filter(PartPrice.tenant_id == tenant_id, PartPrice.deleted_at.is_(None))
        .all()
    )

    count = len(parts)
    if count > 0:
        avg_margin = sum(float(p.margin_pct) for p in parts) / count
    else:
        avg_margin = 0.35  # default 35% if no history

    # Prevent division by zero or nonsensical suggestions
    if avg_margin >= 1.0:
        avg_margin = 0.35
    if avg_margin < 0:
        avg_margin = 0.0

    suggested_sell = body.cost_price / (1.0 - avg_margin) if avg_margin < 1.0 else body.cost_price * 1.35

    return {
        "cost_price": body.cost_price,
        "suggested_sell_price": round(suggested_sell, 2),
        "avg_tenant_margin": round(avg_margin, 4),
        "based_on_parts_count": count,
    }


@router.get("/pricing")
def list_parts(
    request: Request,
    db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> list:
    """List all parts with prices for this tenant."""
    tenant = getattr(request.state, "tenant", None)
    if not tenant:
        raise HTTPException(status_code=400, detail="Tenant context missing")
    tenant_id = str(tenant["id"])

    parts = (
        db.query(PartPrice)
        .filter(PartPrice.tenant_id == tenant_id, PartPrice.deleted_at.is_(None))
        .order_by(asc(PartPrice.part_name))
        .all()
    )
    return [_part_to_dict(p) for p in parts]


@router.post("/pricing", status_code=201)
def create_part(
    body: PartPriceCreate,
    request: Request,
    db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> dict:
    """Create a new part price record."""
    tenant = getattr(request.state, "tenant", None)
    if not tenant:
        raise HTTPException(status_code=400, detail="Tenant context missing")
    tenant_id = str(tenant["id"])

    existing = (
        db.query(PartPrice)
        .filter(
            PartPrice.tenant_id == tenant_id,
            PartPrice.part_number == body.part_number,
            PartPrice.deleted_at.is_(None),
        )
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"Part number '{body.part_number}' already exists")

    now = datetime.now(timezone.utc)
    margin = _compute_margin(body.cost_price, body.sell_price)

    part = PartPrice(
        tenant_id=tenant_id,
        part_number=body.part_number,
        part_name=body.part_name,
        cost_price=body.cost_price,
        sell_price=body.sell_price,
        margin_pct=margin,
        supplier=body.supplier,
        last_updated_at=now,
        price_history=[_history_entry(body.cost_price, body.sell_price, margin)],
        created_at=now,
    )
    db.add(part)
    try:
        db.commit()
        db.refresh(part)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create part: {exc}") from exc

    return _part_to_dict(part, include_history=True)


@router.get("/pricing/{part_number}")
def get_part(
    part_number: str,
    request: Request,
    db: Session = Depends(get_db),
    _: None = _auth_dep,
) -> dict:
    """Get a single part price record including full price history."""
    tenant = getattr(request.state, "tenant", None)
    if not tenant:
        raise HTTPException(status_code=400, detail="Tenant context missing")
    tenant_id = str(tenant["id"])

    part = (
        db.query(PartPrice)
        .filter(
            PartPrice.tenant_id == tenant_id,
            PartPrice.part_number == part_number,
            PartPrice.deleted_at.is_(None),
        )
        .first()
    )
    if not part:
        raise HTTPException(status_code=404, detail=f"Part '{part_number}' not found")

    return _part_to_dict(part, include_history=True)
