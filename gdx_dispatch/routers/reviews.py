from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from statistics import mean
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import CustomerReview, Job
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reviews", tags=["reviews"], dependencies=[Depends(require_module("customer_portal"))])


class ReviewSubmitIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    job_id: str | None = None
    customer_id: str | None = None
    rating: int = Field(ge=1, le=5)
    text: str | None = Field(default=None, max_length=4000)


def _tenant_id(request: Request | None) -> str | None:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    tid = str(tenant.get("id") or "").strip()
    return tid or None


def _actor_id(user: dict[str, Any] | None) -> str:
    user = user or {}
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")


@router.get("", response_model=None)
def list_reviews(
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        reviews = db.execute(
            select(CustomerReview).order_by(CustomerReview.created_at.desc())
        ).scalars().all()
        return {
            "items": [
                {
                    "id": r.id,
                    "tenant_id": r.tenant_id,
                    "job_id": r.job_id,
                    "customer_id": r.customer_id,
                    "token": r.token,
                    "rating": r.rating,
                    "review_text": r.review_text,
                    "status": r.status,
                    "sent_at": r.sent_at,
                    "submitted_at": r.submitted_at,
                    "created_at": r.created_at,
                }
                for r in reviews
            ]
        }
    except Exception:
        log.exception("list_reviews_failed")
        raise HTTPException(status_code=500, detail="Failed to list reviews") from None


@router.post("/request/{job_id}", response_model=None, status_code=201)
def request_review(
    job_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        now = datetime.now(UTC).isoformat()
        review_id = str(uuid4())
        token = uuid4().hex

        try:
            job_uuid = UUID(job_id)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="Invalid job ID") from None
        job = db.execute(
            select(Job).where(Job.id == job_uuid)
        ).scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        review = CustomerReview(
            id=review_id,
            tenant_id=_tenant_id(request),
            company_id=_tenant_id(request),
            job_id=job_id,
            customer_id=str(job.customer_id) if job.customer_id else None,
            token=token,
            rating=None,
            review_text=None,
            status="requested",
            sent_at=now,
            submitted_at=None,
            created_at=now,
        )
        db.add(review)
        asyncio.run(log_audit_event(
            db=db,
            tenant_id=_tenant_id(request),
            company_id=_tenant_id(request),
            user_id=_actor_id(user),
            action="review_requested",
            entity_type="review",
            entity_id=review_id,
            details={"job_id": job_id, "customer_id": str(job.customer_id or "")},
            request=request,
        ))
        db.commit()
        return {"id": review_id, "job_id": job_id, "token": token, "status": "requested"}
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        log.exception("request_review_failed", extra={"job_id": job_id})
        raise HTTPException(status_code=500, detail="Failed to request review") from None


@router.post("", response_model=None, status_code=201)
def submit_review(
    payload: ReviewSubmitIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        now = datetime.now(UTC).isoformat()
        review_id = str(uuid4())
        review = CustomerReview(
            id=review_id,
            tenant_id=_tenant_id(request),
            company_id=_tenant_id(request),
            job_id=payload.job_id,
            customer_id=payload.customer_id,
            token=None,
            rating=payload.rating,
            review_text=payload.text,
            status="submitted",
            sent_at=None,
            submitted_at=now,
            created_at=now,
        )
        db.add(review)
        asyncio.run(log_audit_event(
            db=db,
            tenant_id=_tenant_id(request),
            company_id=_tenant_id(request),
            user_id=_actor_id(user),
            action="review_submitted",
            entity_type="review",
            entity_id=review_id,
            details={"rating": payload.rating, "job_id": payload.job_id},
            request=request,
        ))
        db.commit()
        return {
            "id": review_id,
            "job_id": payload.job_id,
            "customer_id": payload.customer_id,
            "rating": payload.rating,
            "text": payload.text,
            "status": "submitted",
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        log.exception("submit_review_failed")
        raise HTTPException(status_code=500, detail="Failed to submit review") from None


@router.get("/stats", response_model=None)
def review_stats(
    trend_days: int = Query(default=30, ge=1, le=365),
    _: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        rows = db.execute(
            select(CustomerReview.rating, CustomerReview.submitted_at)
            .where(CustomerReview.rating.is_not(None))
            .order_by(CustomerReview.submitted_at.asc())
        ).all()

        ratings = [int(r.rating) for r in rows if r.rating is not None]
        cutoff = datetime.now(UTC) - timedelta(days=trend_days)
        recent = [
            int(r.rating) for r in rows
            if r.rating is not None and r.submitted_at and datetime.fromisoformat(str(r.submitted_at)) >= cutoff
        ]
        older = [
            int(r.rating) for r in rows
            if r.rating is not None and r.submitted_at and datetime.fromisoformat(str(r.submitted_at)) < cutoff
        ]
        avg_recent = mean(recent) if recent else 0.0
        avg_older = mean(older) if older else 0.0

        return {
            "average_rating": round(mean(ratings), 2) if ratings else 0.0,
            "count": len(ratings),
            "trend": round(avg_recent - avg_older, 2),
        }
    except Exception:
        log.exception("review_stats_failed")
        raise HTTPException(status_code=500, detail="Failed to load review stats") from None
