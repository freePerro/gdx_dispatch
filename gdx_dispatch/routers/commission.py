"""Commission — commission rules and earnings tracking per user/job."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import CommissionEntry, CommissionRule
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/commissions",
    tags=["commissions"],
    dependencies=[Depends(require_module("jobs"))],
)


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RuleIn(BaseModel):
    role: str = Field(min_length=1, max_length=100)
    parts_pct: float = Field(default=0, ge=0, le=100)
    labor_pct: float = Field(default=0, ge=0, le=100)
    bonus_per_review: float = Field(default=0, ge=0, le=10_000)


class CalculateIn(BaseModel):
    job_id: str = Field(min_length=1, max_length=36)
    user_id: str = Field(min_length=1, max_length=36)
    role: str = Field(min_length=1, max_length=100)
    parts_total: float = Field(default=0, ge=0, le=10_000_000)
    labor_total: float = Field(default=0, ge=0, le=10_000_000)
    review_count: int = Field(default=0, ge=0, le=10_000)
    period: str = Field(min_length=7, max_length=10, description="YYYY-MM format")


def _serialize_rule(r: CommissionRule) -> dict[str, Any]:
    return {
        "id": str(r.id), "company_id": str(r.company_id), "role": r.role,
        "parts_pct": float(r.parts_pct or 0), "labor_pct": float(r.labor_pct or 0),
        "bonus_per_review": float(r.bonus_per_review or 0),
        "created_at": str(r.created_at) if r.created_at else None,
        "updated_at": str(r.updated_at) if r.updated_at else None,
    }


def _serialize_entry(e: CommissionEntry) -> dict[str, Any]:
    return {
        "id": str(e.id), "company_id": str(e.company_id),
        "user_id": str(e.user_id), "job_id": str(e.job_id),
        "parts_amount": float(e.parts_amount or 0), "labor_amount": float(e.labor_amount or 0),
        "bonus_amount": float(e.bonus_amount or 0), "total": float(e.total or 0),
        "period": e.period,
        "created_at": str(e.created_at) if e.created_at else None,
    }


@router.get("/rules")
def get_rules(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    rules = db.execute(
        select(CommissionRule).order_by(CommissionRule.role)
    ).scalars().all()
    return [_serialize_rule(r) for r in rules]


@router.post("/rules", status_code=201)
def set_rules(
    request: Request,
    payload: RuleIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    uid = _uid(user)
    now = _now()

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    existing = db.execute(
        select(CommissionRule).where(CommissionRule.role == payload.role)
    ).scalar_one_or_none()

    try:
        if existing:
            existing.parts_pct = Decimal(str(payload.parts_pct))
            existing.labor_pct = Decimal(str(payload.labor_pct))
            existing.bonus_per_review = Decimal(str(payload.bonus_per_review))
            existing.updated_at = now
            rule = existing
        else:
            rule = CommissionRule(
                id=uuid4(), company_id=tid, role=payload.role,
                parts_pct=Decimal(str(payload.parts_pct)),
                labor_pct=Decimal(str(payload.labor_pct)),
                bonus_per_review=Decimal(str(payload.bonus_per_review)),
                created_at=now, updated_at=now,
            )
            db.add(rule)
        db.commit()
        db.refresh(rule)
    except Exception:
        db.rollback()
        log.exception("commission_rule_save_failed")
        raise HTTPException(status_code=500, detail="Failed to save commission rule") from None

    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="create" if not existing else "update",
        entity_type="commission_rule", entity_id=str(rule.id),
        details={"role": payload.role, "parts_pct": payload.parts_pct, "labor_pct": payload.labor_pct},
        request=request,
    )
    return _serialize_rule(rule)


@router.get("/earnings")
def get_earnings(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    user_id: str | None = Query(None),
    period: str | None = Query(None, description="YYYY-MM format"),
) -> list[dict[str, Any]]:
    tid = _tid(request)
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    q = select(CommissionEntry)
    if user_id:
        q = q.where(CommissionEntry.user_id == user_id)
    if period:
        q = q.where(CommissionEntry.period == period)
    q = q.order_by(CommissionEntry.created_at.desc())
    return [_serialize_entry(e) for e in db.execute(q).scalars().all()]


@router.post("/calculate", status_code=201)
def calculate_commission(
    request: Request,
    payload: CalculateIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    uid = _uid(user)

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    rule = db.execute(
        select(CommissionRule).where(CommissionRule.role == payload.role)
    ).scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail=f"No commission rule found for role '{payload.role}'")

    parts_pct = float(rule.parts_pct or 0)
    labor_pct = float(rule.labor_pct or 0)
    bonus_per_review = float(rule.bonus_per_review or 0)

    parts_amount = round(payload.parts_total * parts_pct / 100, 2)
    labor_amount = round(payload.labor_total * labor_pct / 100, 2)
    bonus_amount = round(payload.review_count * bonus_per_review, 2)
    total = round(parts_amount + labor_amount + bonus_amount, 2)

    now = _now()
    try:
        entry = CommissionEntry(
            id=uuid4(), company_id=tid, user_id=payload.user_id, job_id=payload.job_id,
            parts_amount=Decimal(str(parts_amount)), labor_amount=Decimal(str(labor_amount)),
            bonus_amount=Decimal(str(bonus_amount)), total=Decimal(str(total)),
            period=payload.period, created_at=now,
        )
        db.add(entry)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("commission_calculate_failed")
        raise HTTPException(status_code=500, detail="Failed to calculate commission") from None

    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="create",
        entity_type="commission_entry", entity_id=str(entry.id),
        details={"job_id": payload.job_id, "target_user": payload.user_id, "total": total, "period": payload.period},
        request=request,
    )
    return {
        "id": str(entry.id), "user_id": payload.user_id, "job_id": payload.job_id,
        "parts_amount": parts_amount, "labor_amount": labor_amount,
        "bonus_amount": bonus_amount, "total": total, "period": payload.period,
    }


@router.get("/summary")
def commission_summary(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    period: str | None = Query(None, description="YYYY-MM format; defaults to current month"),
) -> list[dict[str, Any]]:
    tid = _tid(request)
    if not period:
        period = datetime.now(timezone.utc).strftime("%Y-%m")

    rows = db.execute(
        select(
            CommissionEntry.user_id,
            func.sum(CommissionEntry.parts_amount).label("total_parts"),
            func.sum(CommissionEntry.labor_amount).label("total_labor"),
            func.sum(CommissionEntry.bonus_amount).label("total_bonus"),
            func.sum(CommissionEntry.total).label("grand_total"),
            func.count().label("entry_count"),
        )
        .where(CommissionEntry.company_id == tid, CommissionEntry.period == period)
        .group_by(CommissionEntry.user_id)
        .order_by(func.sum(CommissionEntry.total).desc())
    ).all()
    return [
        {
            "user_id": str(r.user_id),
            "total_parts": float(r.total_parts or 0),
            "total_labor": float(r.total_labor or 0),
            "total_bonus": float(r.total_bonus or 0),
            "grand_total": float(r.grand_total or 0),
            "entry_count": int(r.entry_count),
            "period": period,
        }
        for r in rows
    ]
