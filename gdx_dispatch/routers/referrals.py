from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import LoyaltyReferral
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/referrals", tags=["referrals"], dependencies=[Depends(require_module("loyalty"))])
ALLOWED_STATUSES = {"pending", "converted", "rewarded"}


class ReferralCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    referrer_id: str = Field(min_length=1, max_length=64)
    referee_name: str = Field(min_length=1, max_length=120)
    referee_phone: str = Field(min_length=1, max_length=30)
    referee_email: str | None = Field(default=None, max_length=255)


class ReferralPatchIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    status: str = Field(min_length=1, max_length=20)


def _tenant_id(request: Request | None) -> str | None:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    tid = str(tenant.get("id") or "").strip()
    return tid or None


def _actor_id(user: dict[str, Any] | None) -> str:
    user = user or {}
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")


def _serialize_referral(ref: LoyaltyReferral) -> dict[str, Any]:
    return {
        "id": ref.id,
        "tenant_id": ref.tenant_id,
        "referrer_id": ref.referrer_id,
        "referee_name": ref.referee_name,
        "referee_phone": ref.referee_phone,
        "referee_email": ref.referee_email,
        "status": ref.status,
        "converted_at": ref.converted_at,
        "rewarded_at": ref.rewarded_at,
        "reward_given": ref.reward_given,
        "created_at": ref.created_at,
        "updated_at": ref.updated_at,
    }


def _get_referral_or_404(referral_id: str, db: Session) -> LoyaltyReferral:
    ref = db.execute(
        select(LoyaltyReferral).where(
            LoyaltyReferral.id == referral_id,
            LoyaltyReferral.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not ref:
        raise HTTPException(status_code=404, detail="Referral not found")
    return ref


@router.get("", response_model=None)
def list_referrals(
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        referrals = db.execute(
            select(LoyaltyReferral)
            .where(LoyaltyReferral.deleted_at.is_(None))
            .order_by(LoyaltyReferral.created_at.desc())
        ).scalars().all()
        return {"items": [_serialize_referral(r) for r in referrals]}
    except Exception:
        log.exception("list_referrals_failed")
        raise HTTPException(status_code=500, detail="Failed to list referrals") from None


@router.post("", response_model=None, status_code=201)
def create_referral(
    payload: ReferralCreateIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        referral_id = str(uuid4())
        now = datetime.now(UTC).isoformat()
        ref = LoyaltyReferral(
            id=referral_id,
            tenant_id=_tenant_id(request),
            company_id=_tenant_id(request),
            referrer_id=payload.referrer_id,
            referee_name=payload.referee_name,
            referee_phone=payload.referee_phone,
            referee_email=payload.referee_email,
            status="pending",
            converted_at=None,
            rewarded_at=None,
            reward_given=0,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        db.add(ref)
        db.flush()
        asyncio.run(log_audit_event(
            db=db,
            tenant_id=_tenant_id(request),
            company_id=_tenant_id(request),
            user_id=_actor_id(user),
            action="referral_created",
            entity_type="referral",
            entity_id=referral_id,
            details={"referrer_id": payload.referrer_id},
            request=request,
        ))
        db.commit()
        db.refresh(ref)
        return _serialize_referral(ref)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        log.exception("create_referral_failed")
        raise HTTPException(status_code=500, detail="Failed to create referral") from None


@router.patch("/{referral_id}", response_model=None)
def patch_referral(
    referral_id: str,
    payload: ReferralPatchIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        ref = _get_referral_or_404(referral_id, db)
        new_status = payload.status.lower()
        if new_status not in ALLOWED_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid status")

        order = ["pending", "converted", "rewarded"]
        if order.index(new_status) < order.index(str(ref.status).lower()):
            raise HTTPException(status_code=409, detail="Status cannot move backwards")

        now = datetime.now(UTC).isoformat()
        ref.status = new_status
        ref.updated_at = now
        if new_status in {"converted", "rewarded"} and not ref.converted_at:
            ref.converted_at = now
        if new_status == "rewarded":
            ref.rewarded_at = now
            ref.reward_given = 1

        asyncio.run(log_audit_event(
            db=db,
            tenant_id=_tenant_id(request),
            company_id=_tenant_id(request),
            user_id=_actor_id(user),
            action="referral_updated",
            entity_type="referral",
            entity_id=referral_id,
            details={"status": new_status},
            request=request,
        ))
        db.commit()
        db.refresh(ref)
        return _serialize_referral(ref)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        log.exception("patch_referral_failed", extra={"referral_id": referral_id})
        raise HTTPException(status_code=500, detail="Failed to update referral") from None


@router.get("/stats", response_model=None)
def referral_stats(
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        row = db.execute(
            select(
                func.count().label("total_referrals"),
                func.sum(
                    case(
                        (LoyaltyReferral.status.in_(["converted", "rewarded"]), 1),
                        else_=0,
                    )
                ).label("converted"),
                func.sum(
                    case(
                        (LoyaltyReferral.status == "rewarded", 1),
                        else_=0,
                    )
                ).label("rewards_given"),
            ).where(LoyaltyReferral.deleted_at.is_(None))
        ).first()

        total = int(row.total_referrals or 0) if row else 0
        converted = int(row.converted or 0) if row else 0
        rewards_given = int(row.rewards_given or 0) if row else 0
        conversion_rate = round((converted / total) * 100, 2) if total else 0.0

        return {
            "total_referrals": total,
            "conversion_rate": conversion_rate,
            "rewards_given": rewards_given,
        }
    except Exception:
        log.exception("referral_stats_failed")
        raise HTTPException(status_code=500, detail="Failed to load referral stats") from None
