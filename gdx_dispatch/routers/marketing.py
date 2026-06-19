from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event
from gdx_dispatch.models.tenant_models import LoyaltyPoints, LoyaltyReferral, ReviewRequest

router = APIRouter(prefix="/api", tags=["marketing"])

GOOGLE_REVIEWS_LINK = "https://www.google.com/maps/search/?api=1&query=GDX+Google+Reviews"


class ReferralCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    referrer_customer_id: str = Field(..., min_length=1, max_length=64)
    referee_name: str = Field(..., min_length=1, max_length=120)
    referee_phone: str = Field(..., min_length=1, max_length=30)


def _job_or_404(job_id: str, db: Session) -> dict[str, Any]:
    # Uses text() because Job.id is Uuid(as_uuid=True) which has SQLite/text-insert
    # incompatibility when tests seed via raw SQL with dashed UUID strings.
    row = db.execute(
        text(
            """
            SELECT id, customer_id, title, status, lifecycle_stage, completed_at
            FROM jobs
            WHERE id = :job_id AND deleted_at IS NULL
            LIMIT 1
            """
        ),
        {"job_id": job_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return dict(row)


async def schedule_review_request_for_completed_job(job_id: str, db: Session) -> dict[str, Any]:
    job = _job_or_404(job_id, db)
    status = str(job.get("status") or job.get("lifecycle_stage") or "").lower()
    if status != "completed":
        raise HTTPException(status_code=409, detail="Review requests can only be queued for completed jobs")

    now = datetime.now(UTC)
    scheduled_for = now + timedelta(hours=24)
    message = (
        f"Thanks for choosing GDX. We'd appreciate your feedback: {GOOGLE_REVIEWS_LINK}"
    )

    review_id = str(uuid4())
    review = ReviewRequest(
        id=review_id,
        job_id=job_id,
        customer_id=job.get("customer_id"),
        status="queued",
        message=message,
        google_reviews_link=GOOGLE_REVIEWS_LINK,
        scheduled_for=scheduled_for.isoformat(),
        created_at=now.isoformat(),
    )
    db.add(review)
    db.commit()
    return {
        "id": review_id,
        "job_id": job_id,
        "customer_id": job.get("customer_id"),
        "status": "queued",
        "message": message,
        "scheduled_for": scheduled_for.isoformat(),
    }


# NOTE: /api/reviews/* and /api/referrals/* endpoints moved to dedicated
# gdx_dispatch/routers/reviews.py and gdx_dispatch/routers/referrals.py. The prior inline
# versions here lacked company_id scoping (tenant leak) and collided on
# operation_id. Keeping only convert_referrals_for_customer below as it's
# a helper used by other routers, not an HTTP endpoint.


async def convert_referrals_for_customer(customer_id: str, customer_phone: str, db: Session) -> int:
    if not customer_phone.strip():
        return 0

    now = datetime.now(UTC).isoformat()

    # Fetch pending referrals matching this phone number via ORM
    referrals = db.execute(
        select(LoyaltyReferral).where(
            LoyaltyReferral.status == "pending",
            LoyaltyReferral.referee_phone == customer_phone.strip(),
        )
    ).scalars().all()

    converted = 0
    for referral in referrals:
        # converted_customer_id is not in the ORM model — use text() for this column
        db.execute(
            text(
                """
                UPDATE loyalty_referrals
                SET status = 'converted', converted_customer_id = :customer_id, converted_at = :converted_at
                WHERE id = :referral_id
                """
            ),
            {
                "referral_id": referral.id,
                "customer_id": customer_id,
                "converted_at": now,
            },
        )

        # Award loyalty points via ORM
        points = LoyaltyPoints(
            id=uuid4(),
            customer_id=referral.referrer_id,
            amount=250,
            reason=f"Referral converted: {customer_id}",
            created_by="system",
        )
        db.add(points)

        # Mark referral as rewarded via ORM
        referral.status = "rewarded"
        referral.rewarded_at = now

        await log_audit_event(
            db,
            "referral_converted",
            "system",
            "referral",
            str(referral.id),
            {"referrer_customer_id": referral.referrer_id, "converted_customer_id": customer_id, "points": 250},
        )
        converted += 1

    if converted:
        db.commit()
    return converted
