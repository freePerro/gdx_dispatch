"""Labor pricing matrix admin endpoints — Sprint S97 slice 3.

CRUD over `labor_price_items`. Powers the LaborMatrixView.vue admin surface.

    GET    /api/labor-pricing/items           — list (filterable by active/service_type)
    POST   /api/labor-pricing/items           — create
    GET    /api/labor-pricing/items/{id}      — get one
    PUT    /api/labor-pricing/items/{id}      — replace fields
    DELETE /api/labor-pricing/items/{id}      — soft delete (active=False, effective_to=today)

Per CLAUDE.md AI Access triple-layer: every write is audit-logged and
validated; AI tools call the same typed surface later.

Permission gating: read = `pricing.labor_matrix.read`; write =
`pricing.labor_matrix.write`. Owner/admin pass via the escape hatch in
require_permission.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_permission
from gdx_dispatch.models.labor_pricing import LaborPriceItem
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/labor-pricing", tags=["labor-pricing"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class LaborPriceItemIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    sku: str | None = Field(default=None, max_length=40)
    description: str = Field(min_length=1, max_length=200)
    service_type: str = Field(min_length=1, max_length=40)
    width_ft: int | None = Field(default=None, ge=1, le=40)
    height_ft: int | None = Field(default=None, ge=1, le=40)
    flat_price: float = Field(ge=0)
    # 2026-05-07 — gt=0 (was ge=0) and le=48 (was unbounded). The 700-hour
    # fat-finger that produced EST-000030 line 7 (700 typed where 7 was
    # meant) and the 0-hour 8x7 row both saved through ge=0. No real garage-
    # door scope exceeds 48 man-hours; if it does, build it as multiple rows.
    assumed_man_hours: float = Field(gt=0, le=48)
    default_crew_size: int = Field(default=1, ge=1, le=8)
    min_wall_clock_minutes: int = Field(default=15, ge=15, le=480)
    notes: str | None = None
    active: bool = True
    effective_from: date | None = None
    effective_to: date | None = None
    sort_order: int = 0


# ── Helpers ──────────────────────────────────────────────────────────────────

def _tenant_id(request: Request | None) -> str:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    return str(tenant.get("id") or "")


def _user_id(user: dict | None) -> str:
    if not user:
        return "system"
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")


def _resolve_target_rate(db: Session) -> float:
    """Tenant target blended sell rate per labor man-hour. Drives the
    implied-rate drift warning. Lazily imported to dodge circular imports
    in test harnesses that load this router before pricing_engine."""
    from gdx_dispatch.models.pricing_engine import PricingSettings

    settings = db.execute(select(PricingSettings)).scalar_one_or_none()
    if settings is None or settings.target_labor_blended_rate_per_hour is None:
        return 100.0
    return float(settings.target_labor_blended_rate_per_hour)


def _drift_warning(price: float, hours: float, target: float) -> str | None:
    """≥10% deviation from the tenant's target blended rate is flagged.
    Returns warning string for inclusion in `_warnings`; None if within band."""
    if hours <= 0 or target <= 0:
        return None
    implied = price / hours
    drift = abs(implied - target) / target
    if drift <= 0.10:
        return None
    return (
        f"implied ${implied:.2f}/hr drifts {drift * 100:.0f}% from target "
        f"${target:.2f}/hr — confirm pricing intent"
    )


def _serialize(item: LaborPriceItem, target_rate: float | None = None) -> dict:
    price = float(item.flat_price)
    hours = float(item.assumed_man_hours)
    implied = round(price / hours, 2) if hours > 0 else None
    drift_pct = None
    drift_warn = None
    if target_rate is not None and implied is not None and target_rate > 0:
        drift_pct = round((implied - target_rate) / target_rate * 100, 1)
        drift_warn = _drift_warning(price, hours, target_rate)
    return {
        "id": str(item.id),
        "sku": item.sku,
        "description": item.description,
        "service_type": item.service_type,
        "width_ft": item.width_ft,
        "height_ft": item.height_ft,
        "flat_price": price,
        "assumed_man_hours": hours,
        "default_crew_size": item.default_crew_size,
        "min_wall_clock_minutes": item.min_wall_clock_minutes,
        "implied_hourly_rate": implied,
        "target_hourly_rate": target_rate,
        "hourly_rate_drift_pct": drift_pct,
        "_warnings": [drift_warn] if drift_warn else [],
        "notes": item.notes,
        "active": item.active,
        "effective_from": item.effective_from.isoformat() if item.effective_from else None,
        "effective_to": item.effective_to.isoformat() if item.effective_to else None,
        "sort_order": item.sort_order,
    }


def _validate_size_pair(width: int | None, height: int | None) -> None:
    """Either both size dims are set, or neither. Mixed is a row that
    can't be matched by the size lookup AND has no SKU = unfindable."""
    if (width is None) != (height is None):
        raise HTTPException(
            status_code=422,
            detail="width_ft and height_ft must both be set or both be null",
        )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get(
    "/items",
    response_model=None,
    dependencies=[Depends(require_permission("pricing.labor_matrix.read"))],
)
def list_items(
    active: bool | None = None,
    service_type: str | None = None,
    db: Session = Depends(get_db),
) -> list[dict]:
    """List labor price items, newest first within sort_order.

    Filters: ``active`` (bool), ``service_type`` (exact match).
    """
    q = select(LaborPriceItem)
    if active is not None:
        q = q.where(LaborPriceItem.active == active)
    if service_type:
        q = q.where(LaborPriceItem.service_type == service_type)
    q = q.order_by(
        LaborPriceItem.service_type.asc(),
        LaborPriceItem.sort_order.asc(),
        LaborPriceItem.created_at.desc(),
    )
    rows = db.execute(q).scalars().all()
    target_rate = _resolve_target_rate(db)
    return [_serialize(r, target_rate=target_rate) for r in rows]


@router.post(
    "/items",
    response_model=None,
    dependencies=[Depends(require_permission("pricing.labor_matrix.write"))],
)
def create_item(
    payload: LaborPriceItemIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _validate_size_pair(payload.width_ft, payload.height_ft)

    item = LaborPriceItem(
        sku=payload.sku or None,
        description=payload.description,
        service_type=payload.service_type,
        width_ft=payload.width_ft,
        height_ft=payload.height_ft,
        flat_price=Decimal(str(payload.flat_price)),
        assumed_man_hours=Decimal(str(payload.assumed_man_hours)),
        default_crew_size=payload.default_crew_size,
        min_wall_clock_minutes=payload.min_wall_clock_minutes,
        notes=payload.notes,
        active=payload.active,
        effective_from=payload.effective_from or date.today(),
        effective_to=payload.effective_to,
        sort_order=payload.sort_order,
    )
    db.add(item)
    db.flush()

    log_audit_event_sync(
        db=db,
        tenant_id=_tenant_id(request),
        user_id=_user_id(user),
        action="labor_price_item_created",
        entity_type="labor_price_item",
        entity_id=str(item.id),
        details={
            "service_type": item.service_type,
            "description": item.description,
            "flat_price": float(item.flat_price),
            "assumed_man_hours": float(item.assumed_man_hours),
        },
    )
    db.commit()
    db.refresh(item)
    return _serialize(item, target_rate=_resolve_target_rate(db))


@router.get(
    "/items/{item_id}",
    response_model=None,
    dependencies=[Depends(require_permission("pricing.labor_matrix.read"))],
)
def get_item(item_id: UUID, db: Session = Depends(get_db)) -> dict:
    item = db.get(LaborPriceItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="labor_price_item not found")
    return _serialize(item, target_rate=_resolve_target_rate(db))


@router.put(
    "/items/{item_id}",
    response_model=None,
    dependencies=[Depends(require_permission("pricing.labor_matrix.write"))],
)
def update_item(
    item_id: UUID,
    payload: LaborPriceItemIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    item = db.get(LaborPriceItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="labor_price_item not found")

    _validate_size_pair(payload.width_ft, payload.height_ft)

    item.sku = payload.sku or None
    item.description = payload.description
    item.service_type = payload.service_type
    item.width_ft = payload.width_ft
    item.height_ft = payload.height_ft
    item.flat_price = Decimal(str(payload.flat_price))
    item.assumed_man_hours = Decimal(str(payload.assumed_man_hours))
    item.default_crew_size = payload.default_crew_size
    item.min_wall_clock_minutes = payload.min_wall_clock_minutes
    item.notes = payload.notes
    item.active = payload.active
    if payload.effective_from is not None:
        item.effective_from = payload.effective_from
    item.effective_to = payload.effective_to
    item.sort_order = payload.sort_order

    log_audit_event_sync(
        db=db,
        tenant_id=_tenant_id(request),
        user_id=_user_id(user),
        action="labor_price_item_updated",
        entity_type="labor_price_item",
        entity_id=str(item.id),
        details={
            "service_type": item.service_type,
            "flat_price": float(item.flat_price),
            "assumed_man_hours": float(item.assumed_man_hours),
            "active": item.active,
        },
    )
    db.commit()
    db.refresh(item)
    return _serialize(item, target_rate=_resolve_target_rate(db))


@router.delete(
    "/items/{item_id}",
    response_model=None,
    dependencies=[Depends(require_permission("pricing.labor_matrix.write"))],
)
def archive_item(
    item_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Soft delete: flips active=False and stamps effective_to=today.

    Hard delete is intentionally unavailable — historical EstimateLine
    snapshots reference these rows by FK in slice 4.
    """
    item = db.get(LaborPriceItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="labor_price_item not found")

    item.active = False
    if item.effective_to is None:
        item.effective_to = date.today()

    log_audit_event_sync(
        db=db,
        tenant_id=_tenant_id(request),
        user_id=_user_id(user),
        action="labor_price_item_archived",
        entity_type="labor_price_item",
        entity_id=str(item.id),
        details={"description": item.description},
    )
    db.commit()
    return {"ok": True, "id": str(item.id)}
