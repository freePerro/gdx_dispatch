"""Tax module router.

Endpoints under `/api/tax`. Tenant-plane (per-tenant DB), admin-only.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption
from gdx_dispatch.modules.tax.service import get_or_create_config, resolve_rate
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tax", tags=["tax"])


# ── schemas ────────────────────────────────────────────────────────────


class TaxConfigOut(BaseModel):
    id: str
    name: str
    default_rate: float
    tax_labor: bool = False
    description: str | None = None
    configured_at: datetime | None = None


class TaxConfigPatch(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    default_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    tax_labor: bool | None = None
    description: str | None = None

    @field_validator("default_rate")
    @classmethod
    def _round_4(cls, v: float | None) -> float | None:
        if v is None:
            return None
        # Numeric(5,4) — clip floating-point noise.
        return round(v, 4)


class TaxExemptionOut(BaseModel):
    id: str
    customer_id: str
    exempt: bool
    reason: str | None = None
    certificate_id: str | None = None
    exempt_from: date | None = None
    exempt_until: date | None = None
    notes: str | None = None


class TaxExemptionCreate(BaseModel):
    customer_id: str
    exempt: bool = True
    reason: str | None = None
    certificate_id: str | None = None
    exempt_from: date | None = None
    exempt_until: date | None = None
    notes: str | None = None


# ── helpers ────────────────────────────────────────────────────────────


def _require_admin(user: dict[str, Any]) -> None:
    if (user.get("role") or "").lower() not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="admin role required")


def _config_to_out(c: TaxConfig) -> TaxConfigOut:
    return TaxConfigOut(
        id=str(c.id),
        name=c.name,
        default_rate=float(c.default_rate or 0),
        tax_labor=bool(getattr(c, "tax_labor", False)),
        description=c.description,
        configured_at=c.configured_at,
    )


def _exemption_to_out(e: TaxExemption) -> TaxExemptionOut:
    return TaxExemptionOut(
        id=str(e.id),
        customer_id=str(e.customer_id),
        exempt=bool(e.exempt),
        reason=e.reason,
        certificate_id=e.certificate_id,
        exempt_from=e.exempt_from,
        exempt_until=e.exempt_until,
        notes=e.notes,
    )


# ── endpoints ──────────────────────────────────────────────────────────


@router.get("/config", response_model=TaxConfigOut)
def get_config(
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TaxConfigOut:
    """Read the tenant's default tax rate. Auth required (any role) so the
    invoice-detail UI can render the correct percentage; admin gate is
    only on writes."""
    _ = user
    cfg = get_or_create_config(db)
    return _config_to_out(cfg)


@router.patch("/config", response_model=TaxConfigOut)
def patch_config(
    payload: TaxConfigPatch,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TaxConfigOut:
    _require_admin(user)
    cfg = get_or_create_config(db)
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(cfg, key, value)
    cfg.configured_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(cfg)
    return _config_to_out(cfg)


@router.get("/exemptions", response_model=list[TaxExemptionOut])
def list_exemptions(
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TaxExemptionOut]:
    _require_admin(user)
    rows = db.execute(select(TaxExemption).order_by(TaxExemption.created_at.desc())).scalars().all()
    return [_exemption_to_out(r) for r in rows]


@router.post("/exemptions", response_model=TaxExemptionOut, status_code=201)
def create_exemption(
    payload: TaxExemptionCreate,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TaxExemptionOut:
    _require_admin(user)
    try:
        cid = UUID(payload.customer_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="customer_id must be a UUID")
    row = TaxExemption(
        customer_id=cid,
        exempt=payload.exempt,
        reason=payload.reason,
        certificate_id=payload.certificate_id,
        exempt_from=payload.exempt_from,
        exempt_until=payload.exempt_until,
        notes=payload.notes,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _exemption_to_out(row)


@router.delete("/exemptions/{exemption_id}", status_code=204)
def delete_exemption(
    exemption_id: str,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    _require_admin(user)
    try:
        eid = UUID(exemption_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="exemption not found")
    row = db.execute(select(TaxExemption).where(TaxExemption.id == eid)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="exemption not found")
    db.delete(row)
    db.commit()


@router.get("/resolve")
def resolve_rate_endpoint(
    customer_id: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Diagnostic endpoint — given a customer_id (optional) returns the
    tax rate that would be applied. Used by the invoice-create flow and
    by the Settings → Tax UI to preview the effect of an exemption."""
    _ = user
    rate = resolve_rate(db, customer_id)
    return {"rate": float(rate), "rate_pct": float(rate * 100)}
