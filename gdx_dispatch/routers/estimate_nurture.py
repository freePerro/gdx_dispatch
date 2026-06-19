"""Estimate Nurture — auto follow-up on unsent/declined estimates with rules engine."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import EstimateNurtureLog, EstimateNurtureRule
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/estimate-nurture",
    tags=["estimate-nurture"],
    dependencies=[Depends(require_module("estimates"))],
)


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class NurtureRuleIn(BaseModel):
    delay_hours: int = Field(default=48, ge=1, le=8760)
    message_template: str | None = Field(default=None, max_length=5000)
    discount_pct: float = Field(default=0, ge=0, le=100)
    active: bool = Field(default=True)


def _serialize_rule(r: EstimateNurtureRule) -> dict[str, Any]:
    return {
        "id": str(r.id), "company_id": str(r.company_id),
        "delay_hours": int(r.delay_hours), "message_template": r.message_template,
        "discount_pct": float(r.discount_pct or 0), "active": bool(r.active),
        "created_at": str(r.created_at) if r.created_at else None,
        "updated_at": str(r.updated_at) if r.updated_at else None,
    }


@router.get("/rules")
def get_rules(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    rules = db.execute(
        select(EstimateNurtureRule)
        .order_by(EstimateNurtureRule.delay_hours.asc())
    ).scalars().all()
    return [_serialize_rule(r) for r in rules]


@router.post("/rules", status_code=201)
def create_rule(
    request: Request,
    payload: NurtureRuleIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    uid = _uid(user)
    now = _now()
    try:
        rule = EstimateNurtureRule(
            id=uuid4(), company_id=tid, delay_hours=payload.delay_hours,
            message_template=payload.message_template,
            discount_pct=Decimal(str(payload.discount_pct)),
            active=payload.active, created_at=now, updated_at=now,
        )
        db.add(rule)
        db.commit()
        db.refresh(rule)
    except Exception:
        db.rollback()
        log.exception("nurture_rule_create_failed")
        raise HTTPException(status_code=500, detail="Failed to create nurture rule") from None

    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="create",
        entity_type="estimate_nurture_rule", entity_id=str(rule.id),
        details={"delay_hours": payload.delay_hours, "discount_pct": payload.discount_pct},
        request=request,
    )
    return _serialize_rule(rule)


@router.post("/run")
def run_nurture(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Check unsent/declined estimates, apply nurture rules, log sends."""
    tid = _tid(request)
    uid = _uid(user)
    now = _now()

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    rules = db.execute(
        select(EstimateNurtureRule)
        .where(EstimateNurtureRule.active == True)  # noqa: E712
        .order_by(EstimateNurtureRule.delay_hours.asc())
    ).scalars().all()

    if not rules:
        return {"processed": 0, "sent": 0, "message": "No active nurture rules"}

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    estimates = db.execute(
        select(Estimate).where(
            Estimate.status.in_(["draft", "sent", "declined", "rejected"]),
            Estimate.deleted_at.is_(None),
        ).order_by(Estimate.created_at.asc())
    ).scalars().all()

    sent_count = 0
    processed = 0

    for est in estimates:
        est_id = str(est.id)
        est_created = est.created_at
        if not est_created:
            continue
        processed += 1

        for rule in rules:
            delay_hours = int(rule.delay_hours)
            threshold = now - timedelta(hours=delay_hours)

            try:
                est_dt = datetime.fromisoformat(str(est_created).replace("Z", "+00:00"))
                if est_dt.tzinfo is None:
                    est_dt = est_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                logging.getLogger(__name__).exception("run_nurture caught exception")
                continue

            if est_dt > threshold:
                continue

            # Check if already sent via ORM
            already_sent = db.execute(
                select(EstimateNurtureLog.id)
                .where(EstimateNurtureLog.estimate_id == est_id, EstimateNurtureLog.rule_id == str(rule.id))
            ).first()

            if already_sent:
                continue

            try:
                db.add(EstimateNurtureLog(
                    id=uuid4(), estimate_id=est_id, rule_id=str(rule.id),
                    sent_at=now, channel="email",
                ))
                sent_count += 1
            except Exception:
                log.exception("nurture_log_insert_failed for estimate %s", est_id)

    try:
        db.commit()
    except Exception:
        db.rollback()
        log.exception("nurture_run_commit_failed")
        raise HTTPException(status_code=500, detail="Failed to process nurture run") from None

    if sent_count > 0:
        log_audit_event_sync(
            db, tenant_id=tid, user_id=uid, action="create",
            entity_type="estimate_nurture_run", entity_id=str(uuid4()),
            details={"processed": processed, "sent": sent_count},
            request=request,
        )

    return {"processed": processed, "sent": sent_count}


@router.get("/pending")
def pending_nurtures(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Estimates that are eligible for nurturing soon."""
    tid = _tid(request)
    now = _now()

    # Get smallest active rule delay via ORM
    min_delay_val = db.execute(
        select(func.min(EstimateNurtureRule.delay_hours))
        .where(EstimateNurtureRule.company_id == tid, EstimateNurtureRule.active == True)  # noqa: E712
    ).scalar()

    if min_delay_val is None:
        return []

    min_delay = int(min_delay_val)
    window_start = (now - timedelta(hours=min_delay * 2)).isoformat()

    # Estimates query via ORM
    from datetime import datetime as _dt
    try:
        _window_dt = _dt.fromisoformat(window_start.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        logging.getLogger(__name__).exception("pending_nurtures caught exception")
        _window_dt = now - timedelta(hours=min_delay * 2)
    estimates = db.execute(
        select(Estimate).where(
            Estimate.company_id == tid,
            Estimate.status.in_(["draft", "sent", "declined", "rejected"]),
            Estimate.deleted_at.is_(None),
            Estimate.created_at >= _window_dt,
        ).order_by(Estimate.created_at.asc()).limit(100)
    ).scalars().all()

    result = []
    for est in estimates:
        est_id = str(est.id)
        sends = db.execute(
            select(func.count()).where(EstimateNurtureLog.estimate_id == est_id)
        ).scalar() or 0

        result.append({
            "estimate_id": est_id,
            "customer_id": str(est.customer_id) if est.customer_id else None,
            "status": est.status,
            "created_at": str(est.created_at) if est.created_at else None,
            "total": float(est.total or 0),
            "nurture_sends_completed": sends,
        })
    return result
