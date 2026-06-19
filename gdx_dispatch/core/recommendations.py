"""gdx_dispatch/core/recommendations.py — Next-best-action recommendation engine.

Rule-based recommendations for jobs, customers, operations, and revenue.
Results are cached in Redis with a 1-hour TTL.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid as _uuid_mod
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from redis import Redis, from_url
from sqlalchemy import func
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Customer, Invoice, Job

logger = logging.getLogger(__name__)


def _to_uuid(value: str | Any) -> Any:
    """Convert a string to a UUID object, or None if not a valid UUID.

    Returning None lets callers short-circuit to an empty result instead of
    handing a malformed string to the DB and 500-ing on a Postgres
    InvalidTextRepresentation error.
    """
    if isinstance(value, _uuid_mod.UUID):
        return value
    try:
        return _uuid_mod.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_CACHE_TTL = 3600  # 1 hour


# ---------------------------------------------------------------------------
# Dataclass — recommendation result carrier
# ---------------------------------------------------------------------------

@dataclass
class Recommendation:
    type: str
    title: str
    description: str
    priority: str          # "high" | "medium" | "low"
    action_url: str
    estimated_value: float  # 0.0 when unknown


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _redis() -> Redis:
    return from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
    )


def _cache_get(key: str) -> list[dict] | None:
    try:
        raw = _redis().get(key)
        if raw:
            return json.loads(raw)
    except Exception:
        logging.getLogger(__name__).exception("_cache_get caught exception")
        pass
    return None


def _cache_set(key: str, value: list[dict]) -> None:
    with contextlib.suppress(Exception):
        _redis().setex(key, _CACHE_TTL, json.dumps(value))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# RecommendationEngine
# ---------------------------------------------------------------------------

class RecommendationEngine:
    """Stateless rule-based recommendation engine.

    All methods accept SQLAlchemy Sessions as parameters and are safe to call
    from any context (FastAPI route, background job, tests).
    """

    # ------------------------------------------------------------------
    # Job recommendations
    # ------------------------------------------------------------------

    def get_job_recommendations(
        self,
        tenant_id: str,
        job_id: str,
        tenant_db: Session,
    ) -> list[dict]:
        """Return recommendations specific to a single job."""
        cache_key = f"rec:{tenant_id}:jobs:{job_id}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        results: list[Recommendation] = []
        now = _utcnow()

        job_uuid = _to_uuid(job_id)
        if job_uuid is None:
            return []
        try:
            job = tenant_db.query(Job).filter(
                Job.id == job_uuid,
                Job.deleted_at.is_(None),
            ).first()
            if not job:
                return []

            # Rule: estimate not yet invoiced after 48h
            try:
                if (
                    job.lifecycle_stage == "estimate"
                    and job.created_at is not None
                    and (now - _make_aware(job.created_at)) > timedelta(hours=48)
                ):
                    inv_count = (
                        tenant_db.query(Invoice)
                        .filter(
                            Invoice.job_id == job.id,
                            Invoice.deleted_at.is_(None),
                        )
                        .count()
                    )
                    if inv_count == 0:
                        results.append(Recommendation(
                            type="send_estimate",
                            title="Send Estimate",
                            description=(
                                "This estimate has been open for over 48 hours "
                                "with no invoice sent. Follow up with the customer."
                            ),
                            priority="high",
                            action_url=f"/jobs/{job_id}/invoice/new",
                            estimated_value=0.0,
                        ))
            except Exception as exc:
                logger.warning("send_estimate rule failed for job %s: %s", job_id, exc)

            # Rule: completed job still unbilled
            try:
                if (
                    job.lifecycle_stage == "completed"
                    and job.billing_status == "unbilled"
                ):
                    results.append(Recommendation(
                        type="invoice_now",
                        title="Invoice Now",
                        description=(
                            "This job is completed but has not been invoiced. "
                            "Send the invoice to get paid."
                        ),
                        priority="high",
                        action_url=f"/jobs/{job_id}/invoice/new",
                        estimated_value=0.0,
                    ))
            except Exception as exc:
                logger.warning("invoice_now rule failed for job %s: %s", job_id, exc)

            # Rule: in-progress job scheduled more than 4h ago
            try:
                if (
                    job.lifecycle_stage == "in_progress"
                    and job.scheduled_at is not None
                    and (now - _make_aware(job.scheduled_at)) > timedelta(hours=4)
                ):
                    results.append(Recommendation(
                        type="check_job_status",
                        title="Check Job Status",
                        description=(
                            "This job has been in-progress for more than 4 hours "
                            "past its scheduled time. Check in with the technician."
                        ),
                        priority="medium",
                        action_url=f"/jobs/{job_id}",
                        estimated_value=0.0,
                    ))
            except Exception as exc:
                logger.warning("check_job_status rule failed for job %s: %s", job_id, exc)

        except Exception as exc:
            logger.error("get_job_recommendations failed for job %s: %s", job_id, exc)
            raise RuntimeError(
                f"get_job_recommendations failed for job {job_id}: {exc}"
            ) from exc

        results.sort(key=lambda r: _PRIORITY_ORDER.get(r.priority, 99))
        output = [asdict(r) for r in results]
        _cache_set(cache_key, output)
        return output

    # ------------------------------------------------------------------
    # Customer recommendations
    # ------------------------------------------------------------------

    def get_customer_recommendations(
        self,
        tenant_id: str,
        customer_id: str,
        tenant_db: Session,
    ) -> list[dict]:
        """Return upsell and follow-up recommendations for a customer."""
        cache_key = f"rec:{tenant_id}:customers:{customer_id}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        results: list[Recommendation] = []
        now = _utcnow()
        cutoff_90 = now - timedelta(days=90)
        now - timedelta(days=180)

        cid = _to_uuid(customer_id)
        if cid is None:
            return []
        try:
            customer = tenant_db.query(Customer).filter(
                Customer.id == cid,
                Customer.deleted_at.is_(None),
            ).first()
            if not customer:
                return []

            # All completed jobs for this customer
            completed_jobs = (
                tenant_db.query(Job)
                .filter(
                    Job.customer_id == cid,
                    Job.lifecycle_stage == "completed",
                    Job.deleted_at.is_(None),
                )
                .all()
            )
            total_completed = len(completed_jobs)

            # Rule: repeat customer with no recent activity → annual maintenance follow-up
            try:
                if total_completed > 2:
                    recent_job = (
                        tenant_db.query(Job)
                        .filter(
                            Job.customer_id == cid,
                            Job.created_at >= cutoff_90,
                            Job.deleted_at.is_(None),
                        )
                        .first()
                    )
                    if recent_job is None:
                        results.append(Recommendation(
                            type="annual_maintenance_followup",
                            title="Annual Maintenance Follow-Up",
                            description=(
                                f"This customer has {total_completed} completed jobs "
                                "but no activity in the past 90 days. "
                                "Reach out to schedule annual maintenance."
                            ),
                            priority="medium",
                            action_url=f"/customers/{customer_id}/schedule",
                            estimated_value=150.0,
                        ))
            except Exception as exc:
                logger.warning(
                    "annual_maintenance_followup rule failed for customer %s: %s",
                    customer_id, exc,
                )

            # Rule: high-value customer without maintenance plan → upsell
            try:
                avg_total = (
                    tenant_db.query(func.avg(Invoice.total))
                    .join(Job, Invoice.job_id == Job.id)
                    .filter(
                        Job.customer_id == cid,
                        Invoice.status == "paid",
                        Invoice.deleted_at.is_(None),
                    )
                    .scalar()
                )
                if avg_total is not None and float(avg_total) > 500.0:
                    has_maintenance = (
                        tenant_db.query(Job)
                        .filter(
                            Job.customer_id == cid,
                            Job.title.ilike("%maintenance%"),
                            Job.deleted_at.is_(None),
                        )
                        .first()
                    )
                    if not has_maintenance:
                        results.append(Recommendation(
                            type="upsell_maintenance_plan",
                            title="Upsell Maintenance Plan",
                            description=(
                                f"This customer's average invoice is ${float(avg_total):.0f} — "
                                "they are a strong candidate for a recurring maintenance plan."
                            ),
                            priority="high",
                            action_url=f"/customers/{customer_id}/schedule",
                            estimated_value=float(avg_total) * 0.5,
                        ))
            except Exception as exc:
                logger.warning(
                    "upsell_maintenance_plan rule failed for customer %s: %s",
                    customer_id, exc,
                )

            # Rule: single-job customer → request a review
            try:
                all_jobs_count = (
                    tenant_db.query(Job)
                    .filter(
                        Job.customer_id == cid,
                        Job.deleted_at.is_(None),
                    )
                    .count()
                )
                if all_jobs_count == 1 and total_completed == 1:
                    results.append(Recommendation(
                        type="request_review",
                        title="Request a Review",
                        description=(
                            "This customer completed their first job. "
                            "Now is the ideal time to ask for a review."
                        ),
                        priority="low",
                        action_url=f"/customers/{customer_id}/message",
                        estimated_value=0.0,
                    ))
            except Exception as exc:
                logger.warning(
                    "request_review rule failed for customer %s: %s",
                    customer_id, exc,
                )

        except Exception as exc:
            logger.error(
                "get_customer_recommendations failed for customer %s: %s",
                customer_id, exc,
            )
            return []

        results.sort(key=lambda r: _PRIORITY_ORDER.get(r.priority, 99))
        output = [asdict(r) for r in results]
        _cache_set(cache_key, output)
        return output

    # ------------------------------------------------------------------
    # Operational recommendations
    # ------------------------------------------------------------------

    def get_operational_recommendations(
        self,
        tenant_id: str,
        tenant_db: Session,
    ) -> list[dict]:
        """Return staffing and scheduling recommendations for the tenant."""
        cache_key = f"rec:{tenant_id}:operational"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        results: list[Recommendation] = []
        now = _utcnow()
        week_start = now - timedelta(days=7)
        cutoff_7 = now - timedelta(days=7)

        try:
            # Rule: too many unassigned jobs
            try:
                unassigned_count = (
                    tenant_db.query(Job)
                    .filter(
                        Job.dispatch_status == "unassigned",
                        Job.lifecycle_stage.in_(["scheduled", "in_progress"]),
                        Job.deleted_at.is_(None),
                    )
                    .count()
                )
                if unassigned_count > 3:
                    results.append(Recommendation(
                        type="unassigned_jobs_alert",
                        title="Staff Alert: Unassigned Jobs",
                        description=(
                            f"{unassigned_count} jobs are scheduled or in-progress "
                            "but have no technician assigned. Assign technicians now."
                        ),
                        priority="high",
                        action_url="/dispatch",
                        estimated_value=0.0,
                    ))
            except Exception as exc:
                logger.warning(
                    "unassigned_jobs_alert rule failed for tenant %s: %s",
                    tenant_id, exc,
                )

            # Rule: overloaded technician this week
            try:
                overloaded = (
                    tenant_db.query(
                        Job.assigned_to,
                        func.count(Job.id).label("job_count"),
                    )
                    .filter(
                        Job.created_at >= week_start,
                        Job.deleted_at.is_(None),
                        Job.assigned_to.isnot(None),
                    )
                    .group_by(Job.assigned_to)
                    .having(func.count(Job.id) > 8)
                    .first()
                )
                if overloaded:
                    results.append(Recommendation(
                        type="technician_overloaded",
                        title="Technician Overloaded",
                        description=(
                            f"Technician {overloaded.assigned_to} has "
                            f"{overloaded.job_count} jobs this week — "
                            "consider redistributing workload."
                        ),
                        priority="high",
                        action_url="/team",
                        estimated_value=0.0,
                    ))
            except Exception as exc:
                logger.warning(
                    "technician_overloaded rule failed for tenant %s: %s",
                    tenant_id, exc,
                )

            # Rule: stale estimates older than 7 days
            try:
                stale_estimates = (
                    tenant_db.query(Job)
                    .filter(
                        Job.lifecycle_stage == "estimate",
                        Job.created_at < cutoff_7,
                        Job.deleted_at.is_(None),
                    )
                    .count()
                )
                if stale_estimates > 5:
                    results.append(Recommendation(
                        type="follow_up_estimates",
                        title="Follow Up on Stale Estimates",
                        description=(
                            f"{stale_estimates} estimates are more than 7 days old "
                            "with no progress. Follow up to improve close rate."
                        ),
                        priority="medium",
                        action_url="/jobs?stage=estimate",
                        estimated_value=0.0,
                    ))
            except Exception as exc:
                logger.warning(
                    "follow_up_estimates rule failed for tenant %s: %s",
                    tenant_id, exc,
                )

        except Exception as exc:
            logger.error(
                "get_operational_recommendations failed for tenant %s: %s",
                tenant_id, exc,
            )
            return []

        results.sort(key=lambda r: _PRIORITY_ORDER.get(r.priority, 99))
        output = [asdict(r) for r in results]
        _cache_set(cache_key, output)
        return output

    # ------------------------------------------------------------------
    # Revenue recommendations
    # ------------------------------------------------------------------

    def get_revenue_recommendations(
        self,
        tenant_id: str,
        tenant_db: Session,
    ) -> list[dict]:
        """Return pricing and service mix revenue recommendations."""
        cache_key = f"rec:{tenant_id}:revenue"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        results: list[Recommendation] = []
        now = _utcnow()
        cutoff_30 = now - timedelta(days=30)
        cutoff_60 = now - timedelta(days=60)
        cutoff_90 = now - timedelta(days=90)

        try:
            # Rule: low average invoice value
            try:
                avg_paid = (
                    tenant_db.query(func.avg(Invoice.total))
                    .filter(
                        Invoice.status == "paid",
                        Invoice.paid_at >= cutoff_90,
                        Invoice.deleted_at.is_(None),
                    )
                    .scalar()
                )
                if avg_paid is not None and float(avg_paid) < 200.0:
                    results.append(Recommendation(
                        type="review_pricing",
                        title="Review Your Pricing",
                        description=(
                            f"Your average paid invoice is ${float(avg_paid):.0f} over "
                            "the last 90 days — below the $200 threshold. "
                            "Consider reviewing your service pricing."
                        ),
                        priority="low",
                        action_url="/settings/pricing",
                        estimated_value=0.0,
                    ))
            except Exception as exc:
                logger.warning(
                    "review_pricing rule failed for tenant %s: %s",
                    tenant_id, exc,
                )

            # Rule: unbilled completed work
            try:
                unbilled_count = (
                    tenant_db.query(Job)
                    .filter(
                        Job.lifecycle_stage == "completed",
                        Job.billing_status == "unbilled",
                        Job.completed_at >= cutoff_30,
                        Job.deleted_at.is_(None),
                    )
                    .count()
                )
                if unbilled_count > 10:
                    results.append(Recommendation(
                        type="unbilled_work_alert",
                        title="Unbilled Work Alert",
                        description=(
                            f"{unbilled_count} completed jobs in the last 30 days "
                            "have not been invoiced. Bill this work to capture revenue."
                        ),
                        priority="high",
                        action_url="/jobs?billing_status=unbilled&stage=completed",
                        estimated_value=0.0,
                    ))
            except Exception as exc:
                logger.warning(
                    "unbilled_work_alert rule failed for tenant %s: %s",
                    tenant_id, exc,
                )

            # Rule: month-over-month job count decline
            try:
                jobs_last_30 = (
                    tenant_db.query(Job)
                    .filter(
                        Job.created_at >= cutoff_30,
                        Job.deleted_at.is_(None),
                    )
                    .count()
                )
                jobs_prior_30 = (
                    tenant_db.query(Job)
                    .filter(
                        Job.created_at >= cutoff_60,
                        Job.created_at < cutoff_30,
                        Job.deleted_at.is_(None),
                    )
                    .count()
                )
                if jobs_prior_30 > 0 and jobs_last_30 < jobs_prior_30 * 0.8:
                    decline_pct = int((1 - jobs_last_30 / jobs_prior_30) * 100)
                    results.append(Recommendation(
                        type="revenue_trend_alert",
                        title="Revenue Trend Alert",
                        description=(
                            f"Job volume is down {decline_pct}% compared to the prior "
                            "30 days. Review your lead sources and marketing."
                        ),
                        priority="medium",
                        action_url="/reports/revenue",
                        estimated_value=0.0,
                    ))
            except Exception as exc:
                logger.warning(
                    "revenue_trend_alert rule failed for tenant %s: %s",
                    tenant_id, exc,
                )

        except Exception as exc:
            logger.error(
                "get_revenue_recommendations failed for tenant %s: %s",
                tenant_id, exc,
            )
            return []

        results.sort(key=lambda r: _PRIORITY_ORDER.get(r.priority, 99))
        output = [asdict(r) for r in results]
        _cache_set(cache_key, output)
        return output


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_aware(dt: datetime) -> datetime:
    """Return a timezone-aware UTC datetime, converting naive datetimes."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# Module-level singleton
engine = RecommendationEngine()
