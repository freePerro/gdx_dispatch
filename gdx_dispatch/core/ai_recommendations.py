"""gdx_dispatch/core/ai_recommendations.py — AI-powered business recommendations for tenants.

Analyses usage signals and returns actionable recommendations.
Dismissed recommendations are stored in Redis with a 30-day TTL.
"""
from __future__ import annotations

import logging
import os
import uuid as _uuid_mod
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, Request
from redis import Redis, from_url
from sqlalchemy import func
from sqlalchemy.orm import Session

from gdx_dispatch.control.models import TenantModuleGrant
from gdx_dispatch.core.database import get_db, get_db
from gdx_dispatch.models.tenant_models import Invoice, Job

logger = logging.getLogger(__name__)

INDUSTRY_BENCHMARK_JOB_VALUE = 285.0  # dollars

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class Recommendation:
    type: str
    priority: str  # "high" | "medium" | "low"
    title: str
    body: str
    action_url: str
    metric: Any  # the triggering metric value


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _redis() -> Redis:
    return from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)


# ---------------------------------------------------------------------------
# Core recommendation engine
# ---------------------------------------------------------------------------

def _to_uuid(tenant_id: str) -> _uuid_mod.UUID:
    """Convert tenant_id string to UUID for ORM UUID-column comparisons."""
    if isinstance(tenant_id, _uuid_mod.UUID):
        return tenant_id
    return _uuid_mod.UUID(tenant_id)


def _has_module(control_db: Session, tenant_id: str, module_key: str) -> bool:
    """Return True if the tenant has the given module granted."""
    try:
        tid = _to_uuid(tenant_id)
    except ValueError:  # Return False if tenant_id is not a valid UUID
        logging.getLogger(__name__).exception("_has_module caught exception")
        return False
    return (
        control_db.query(TenantModuleGrant)  # noqa: T1 — TenantModuleGrant is control-plane (shared DB)
        .filter(
            TenantModuleGrant.tenant_id == tid,
            TenantModuleGrant.module_key == module_key,
        )
        .first()
    ) is not None


def get_recommendations(
    tenant_id: str,
    tenant_db: Session,
    control_db: Session,
) -> list[Recommendation]:
    """Evaluate all recommendation rules and return a sorted list."""
    results: list[Recommendation] = []
    try:
        now = datetime.now(timezone.utc)
        cutoff_30 = now - timedelta(days=30)
        cutoff_7 = now - timedelta(days=7)
        cutoff_90 = now - timedelta(days=90)

        # ------------------------------------------------------------------
        # Rule: enable_module — high job volume but no inventory module
        # ------------------------------------------------------------------
        try:
            jobs_30d = (
                tenant_db.query(Job)
                .filter(Job.created_at >= cutoff_30, Job.deleted_at.is_(None))
                .count()
            )
            has_inventory = _has_module(control_db, tenant_id, "inventory")
            if jobs_30d > 10 and not has_inventory:
                results.append(
                    Recommendation(
                        type="enable_module",
                        priority="medium",
                        title="Enable Inventory Module",
                        body=(
                            f"You have {jobs_30d} jobs/month but aren't tracking parts — "
                            "enable inventory to reduce material waste."
                        ),
                        action_url="/settings/modules",
                        metric=jobs_30d,
                    )
                )
        except Exception as exc:
            logger.warning("enable_module rule failed for %s: %s", tenant_id, exc)

        # ------------------------------------------------------------------
        # Rule: activate_campaigns — sent invoices (estimates) not yet paid in last 30d
        # ------------------------------------------------------------------
        try:
            unsold_estimates = (
                tenant_db.query(Invoice)
                .filter(
                    Invoice.status == "sent",
                    Invoice.sent_at >= cutoff_30,
                    Invoice.deleted_at.is_(None),
                )
                .count()
            )
            if unsold_estimates > 5:
                results.append(
                    Recommendation(
                        type="activate_campaigns",
                        priority="high",
                        title="Re-engage Unsold Estimates",
                        body=(
                            f"{unsold_estimates} estimates haven't converted in 30 days — "
                            "launch a follow-up campaign."
                        ),
                        action_url="/campaigns/new",
                        metric=unsold_estimates,
                    )
                )
        except Exception as exc:
            logger.warning("activate_campaigns rule failed for %s: %s", tenant_id, exc)

        # ------------------------------------------------------------------
        # Rule: setup_maintenance_plans — repeat customers without plan
        # ------------------------------------------------------------------
        try:
            if not _has_module(control_db, tenant_id, "maintenance_plans"):
                repeat_customers = (
                    tenant_db.query(Job.customer_id)
                    .filter(Job.deleted_at.is_(None))
                    .group_by(Job.customer_id)
                    .having(func.count(Job.id) > 2)
                    .count()
                )
                if repeat_customers > 3:
                    results.append(
                        Recommendation(
                            type="setup_maintenance_plans",
                            priority="medium",
                            title="Set Up Maintenance Plans",
                            body=(
                                f"{repeat_customers} repeat customers have no maintenance plan — "
                                "set up recurring revenue."
                            ),
                            action_url="/settings/modules",
                            metric=repeat_customers,
                        )
                    )
        except Exception as exc:
            logger.warning("setup_maintenance_plans rule failed for %s: %s", tenant_id, exc)

        # ------------------------------------------------------------------
        # Rule: connect_qb — invoices exist but no QB module
        # ------------------------------------------------------------------
        try:
            if not _has_module(control_db, tenant_id, "quickbooks"):
                invoice_count = (
                    tenant_db.query(Invoice)
                    .filter(Invoice.deleted_at.is_(None))
                    .count()
                )
                if invoice_count > 0:
                    results.append(
                        Recommendation(
                            type="connect_qb",
                            priority="high",
                            title="Connect QuickBooks",
                            body=(
                                f"You have {invoice_count} invoices but no accounting sync — "
                                "connect QuickBooks to save hours."
                            ),
                            action_url="/settings/integrations",
                            metric=invoice_count,
                        )
                    )
        except Exception as exc:
            logger.warning("connect_qb rule failed for %s: %s", tenant_id, exc)

        # ------------------------------------------------------------------
        # Rule: increase_prices — avg job value below benchmark
        # ------------------------------------------------------------------
        try:
            avg_value = (
                tenant_db.query(func.avg(Invoice.total))
                .filter(
                    Invoice.status == "paid",
                    Invoice.paid_at >= cutoff_90,
                    Invoice.deleted_at.is_(None),
                )
                .scalar()
            )
            if avg_value is not None and float(avg_value) < INDUSTRY_BENCHMARK_JOB_VALUE:
                avg_val = float(avg_value)
                results.append(
                    Recommendation(
                        type="increase_prices",
                        priority="low",
                        title="Review Your Pricing",
                        body=(
                            f"Your avg job value (${avg_val:.0f}) is below the industry "
                            f"benchmark (${INDUSTRY_BENCHMARK_JOB_VALUE:.0f})."
                        ),
                        action_url="/settings/pricing",
                        metric=round(avg_val, 2),
                    )
                )
        except Exception as exc:
            logger.warning("increase_prices rule failed for %s: %s", tenant_id, exc)

        # ------------------------------------------------------------------
        # Rule: hire_technician — single tech with >15 jobs in last 7d
        # ------------------------------------------------------------------
        try:
            overloaded = (
                tenant_db.query(
                    Job.assigned_to,
                    func.count(Job.id).label("job_count"),
                )
                .filter(
                    Job.created_at >= cutoff_7,
                    Job.deleted_at.is_(None),
                    Job.assigned_to.isnot(None),
                )
                .group_by(Job.assigned_to)
                .having(func.count(Job.id) > 15)
                .first()
            )
            if overloaded:
                results.append(
                    Recommendation(
                        type="hire_technician",
                        priority="high",
                        title="Technician Overloaded",
                        body=(
                            f"Tech {overloaded.assigned_to} has >15 jobs this week — "
                            "consider hiring."
                        ),
                        action_url="/team",
                        metric=str(overloaded.assigned_to),
                    )
                )
        except Exception as exc:
            logger.warning("hire_technician rule failed for %s: %s", tenant_id, exc)

    except Exception as exc:
        logger.error("get_recommendations failed for %s: %s", tenant_id, exc)
        return []

    # Sort: high → medium → low
    results.sort(key=lambda r: _PRIORITY_ORDER.get(r.priority, 99))
    return results


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api", tags=["recommendations"])


@router.get("/recommendations")
def list_recommendations(
    request: Request,
    tenant_db: Session = Depends(get_db),
    control_db: Session = Depends(get_db),
) -> list[dict]:
    """Return AI-powered recommendations for the current tenant, excluding dismissed ones."""
    tenant = getattr(request.state, "tenant", None) or {}
    tenant_id = str(tenant.get("id", ""))
    if not tenant_id:
        return []

    recs = get_recommendations(tenant_id, tenant_db, control_db)

    # Filter out dismissed
    active: list[dict] = []
    for rec in recs:
        dismiss_key = f"rec:dismissed:{tenant_id}:{rec.type}"
        try:
            dismissed = _redis().get(dismiss_key)
        except Exception:
            logging.getLogger(__name__).exception("list_recommendations caught exception")
            dismissed = None
        if not dismissed:
            active.append(asdict(rec))
    return active


@router.post("/recommendations/{rec_type}/dismiss")
def dismiss_recommendation(
    rec_type: str,
    request: Request,
) -> dict:
    """Mark a recommendation as dismissed for 30 days."""
    tenant = getattr(request.state, "tenant", None) or {}
    tenant_id = str(tenant.get("id", ""))
    if not tenant_id:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Missing tenant context")

    dismiss_key = f"rec:dismissed:{tenant_id}:{rec_type}"
    try:
        _redis().setex(dismiss_key, 2_592_000, "1")  # 30 days
    except Exception as exc:
        logger.warning("Failed to persist dismissal for %s/%s: %s", tenant_id, rec_type, exc)

    return {"dismissed": rec_type}
