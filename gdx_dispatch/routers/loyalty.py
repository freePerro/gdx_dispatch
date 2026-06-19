from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import LoyaltyPoints, LoyaltyReferral, LoyaltyTier
from gdx_dispatch.routers.auth import get_current_user

try:
    from gdx_dispatch.core.modules import require_module
except ImportError:  # fallback to no-op dependency if module is missing
    logging.getLogger(__name__).exception("loyalty_modules_import_failed_using_fallback")
    def require_module(_: str):  # type: ignore[misc]
        def _dependency() -> None:
            return None
        return _dependency

require_loyalty_module = require_module("loyalty")
log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/loyalty",
    tags=["loyalty"],
    dependencies=[Depends(require_loyalty_module)],
)

DEFAULT_TIERS = [
    {"name": "bronze", "min_spend": 0.0, "discount_pct": 0.0},
    {"name": "silver", "min_spend": 1000.0, "discount_pct": 5.0},
    {"name": "gold", "min_spend": 5000.0, "discount_pct": 10.0},
    {"name": "platinum", "min_spend": 10000.0, "discount_pct": 15.0},
]


def _serialize_tier(tier: LoyaltyTier) -> dict[str, Any]:
    return {
        "id": str(tier.id),
        "name": tier.name,
        "min_spend": float(tier.min_spend),
        "discount_pct": float(tier.discount_pct),
        "created_at": tier.created_at.isoformat() if tier.created_at else None,
    }


def _serialize_referral(ref: LoyaltyReferral) -> dict[str, Any]:
    return {
        "id": str(ref.id),
        "referrer_id": ref.referrer_id,
        "referee_name": ref.referee_name,
        "referee_phone": ref.referee_phone,
        "status": ref.status,
        "created_at": (ref.created_at.isoformat() if hasattr(ref.created_at, "isoformat") else ref.created_at) if ref.created_at else None,
    }


def _resolve_tier(points: int, db: Session) -> dict[str, Any] | None:
    tiers = db.query(LoyaltyTier).order_by(LoyaltyTier.min_spend.asc()).all()
    tier_list = [
        {
            "name": row.name,
            "min_spend": float(row.min_spend),
            "discount_pct": float(row.discount_pct),
        }
        for row in tiers
    ] or DEFAULT_TIERS

    matches = [tier for tier in tier_list if points >= int(tier["min_spend"])]
    return matches[-1] if matches else None


class TierCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    min_spend: Decimal = Field(ge=0)
    discount_pct: Decimal = Field(ge=0, le=100)


class TierPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    min_spend: Decimal | None = Field(default=None, ge=0)
    discount_pct: Decimal | None = Field(default=None, ge=0, le=100)


class PointsAward(BaseModel):
    amount: int = Field(le=10_000_000)
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("amount")
    @classmethod
    def _amount_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("amount must be greater than 0")
        return value


class ReferralCreate(BaseModel):
    referrer_id: str = Field(min_length=1, max_length=64)
    referee_name: str = Field(min_length=1, max_length=100)
    referee_phone: str = Field(min_length=1, max_length=30)


@router.get("/tiers")
def list_tiers(
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    tiers = db.query(LoyaltyTier).order_by(LoyaltyTier.min_spend.asc()).all()
    if not tiers:
        return DEFAULT_TIERS
    return [_serialize_tier(row) for row in tiers]


@router.post("/tiers", status_code=201)
def create_tier(
    payload: TierCreate,
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tier = LoyaltyTier(
        name=payload.name.strip().lower(),
        min_spend=payload.min_spend,
        discount_pct=payload.discount_pct,
    )
    db.add(tier)
    db.commit()
    db.refresh(tier)
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
                action="create_tier",
                entity_type="tier",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('create_tier_audit_failed')
    return _serialize_tier(tier)


@router.get("/tiers/{tier_id}")
def get_tier(
    tier_id: UUID,
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.get(LoyaltyTier, tier_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Tier not found")
    return _serialize_tier(row)


@router.patch("/tiers/{tier_id}")
def update_tier(
    tier_id: UUID,
    payload: TierPatch = Body(...),
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.get(LoyaltyTier, tier_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Tier not found")

    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    if "name" in updates:
        row.name = str(updates["name"]).strip().lower()
    if "min_spend" in updates:
        row.min_spend = updates["min_spend"]
    if "discount_pct" in updates:
        row.discount_pct = updates["discount_pct"]

    db.add(row)
    db.commit()
    db.refresh(row)
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
                action="update_tier",
                entity_type="tier",
                entity_id=str(tier_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('update_tier_audit_failed')
    return _serialize_tier(row)


@router.get("/customers/{customer_id}/points")
def get_customer_points(
    customer_id: str,
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    total = db.query(func.coalesce(func.sum(LoyaltyPoints.amount), 0)).filter(
        LoyaltyPoints.customer_id == customer_id
    ).scalar()
    return {"customer_id": customer_id, "points": int(total or 0)}


@router.post("/customers/{customer_id}/points", status_code=201)
def award_points(
    customer_id: str,
    payload: PointsAward,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    entry = LoyaltyPoints(
        customer_id=customer_id,
        amount=payload.amount,
        reason=payload.reason.strip(),
        created_by=str(user.get("user_id") or user.get("id") or ""),
        created_at=datetime.now(UTC),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    log.info("loyalty_points_awarded", extra={"customer_id": customer_id, "amount": payload.amount})
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
                action="award_points",
                entity_type="award_point",
                entity_id=str(customer_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('award_points_audit_failed')
    return {
        "id": str(entry.id),
        "customer_id": entry.customer_id,
        "amount": entry.amount,
        "reason": entry.reason,
        "created_by": entry.created_by,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }


@router.get("/customers/{customer_id}/tier")
def get_customer_tier(
    customer_id: str,
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    total = db.query(func.coalesce(func.sum(LoyaltyPoints.amount), 0)).filter(
        LoyaltyPoints.customer_id == customer_id
    ).scalar()
    points = int(total or 0)
    return {
        "customer_id": customer_id,
        "points": points,
        "tier": _resolve_tier(points, db),
    }


@router.get("/referrals")
def list_referrals(
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.query(LoyaltyReferral).order_by(LoyaltyReferral.created_at.desc()).all()
    return [_serialize_referral(row) for row in rows]


@router.post("/referrals", status_code=201)
def create_referral(
    payload: ReferralCreate,
    request: Request,
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _tid = str((getattr(request.state, "tenant", {}) or {}).get("id") or "tenant-test")
    ref = LoyaltyReferral(
        company_id=_tid,
        referrer_id=payload.referrer_id,
        referee_name=payload.referee_name.strip(),
        referee_phone=payload.referee_phone.strip(),
    )
    db.add(ref)
    db.commit()
    db.refresh(ref)
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
                action="create_referral",
                entity_type="referral",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('create_referral_audit_failed')
    return _serialize_referral(ref)
