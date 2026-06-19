"""gdx_dispatch/core/tenant_metrics.py — Tenant usage metrics dashboard.

Computes per-tenant operational metrics over configurable time windows
and exposes them via admin-only FastAPI endpoints.
"""
from __future__ import annotations

import contextlib
import logging
import os
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session, sessionmaker

from gdx_dispatch.control.models import Tenant
from gdx_dispatch.core.database import SessionLocal, get_db
from gdx_dispatch.core.modules import require_role
from gdx_dispatch.models.tenant_models import Invoice, Job

logger = logging.getLogger(__name__)

_PERIOD_DAYS: dict[str, int] = {"7d": 7, "30d": 30, "90d": 90}


# ---------------------------------------------------------------------------
# Dataclass — lightweight result carrier
# ---------------------------------------------------------------------------

@dataclass
class TenantMetrics:
    tenant_id: str
    period: str
    jobs_created: int
    jobs_completed: int
    revenue_total: float
    active_customers: int
    avg_job_value: float
    top_technician: str | None
    busiest_day: str | None
    computed_at: datetime


# ---------------------------------------------------------------------------
# Tenant DB connector (mirrors health_score.py pattern)
# ---------------------------------------------------------------------------

def _connect_tenant_db() -> Session:
    return SessionLocal()


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def get_tenant_metrics(
    tenant_id: str,
    period: str = "30d",
    db: Session | None = None,
) -> TenantMetrics:
    """Compute usage metrics for a tenant over the given period."""
    days = _PERIOD_DAYS.get(period, 30)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    jobs_created = 0
    jobs_completed = 0
    revenue_total = 0.0
    active_customers = 0
    top_technician: str | None = None
    busiest_day: str | None = None

    if db is None:
        return TenantMetrics(
            tenant_id=tenant_id,
            period=period,
            jobs_created=0,
            jobs_completed=0,
            revenue_total=0.0,
            active_customers=0,
            avg_job_value=0.0,
            top_technician=None,
            busiest_day=None,
            computed_at=now,
        )

    # jobs created in period
    with contextlib.suppress(Exception):
        jobs_created = (
            db.query(Job)
            .filter(Job.created_at >= cutoff, Job.deleted_at.is_(None))
            .count()
        )

    # jobs completed in period
    with contextlib.suppress(Exception):
        jobs_completed = (
            db.query(Job)
            .filter(
                Job.lifecycle_stage == "completed",
                Job.completed_at >= cutoff,
                Job.deleted_at.is_(None),
            )
            .count()
        )

    # revenue: sum of invoice totals paid in period
    try:
        result = (
            db.query(func.coalesce(func.sum(Invoice.total), 0.0))
            .filter(Invoice.paid_at >= cutoff, Invoice.deleted_at.is_(None))
            .scalar()
        )
        revenue_total = float(result or 0.0)
    except Exception:
        logging.getLogger(__name__).exception("get_tenant_metrics caught exception")
        pass

    # active customers: distinct customer_ids with jobs in period
    with contextlib.suppress(Exception):
        active_customers = (
            db.query(Job.customer_id)
            .filter(Job.created_at >= cutoff, Job.deleted_at.is_(None))
            .distinct()
            .count()
        )

    # top technician: most common assigned_to in completed jobs
    try:
        row = (
            db.query(Job.assigned_to, func.count(Job.id).label("cnt"))
            .filter(
                Job.lifecycle_stage == "completed",
                Job.completed_at >= cutoff,
                Job.deleted_at.is_(None),
                Job.assigned_to.isnot(None),
            )
            .group_by(Job.assigned_to)
            .order_by(func.count(Job.id).desc())
            .first()
        )
        top_technician = row[0] if row else None
    except Exception:
        logging.getLogger(__name__).exception("get_tenant_metrics caught exception")
        pass

    # busiest day: day of week with most jobs created
    try:
        created_jobs = (
            db.query(Job.created_at)
            .filter(Job.created_at >= cutoff, Job.deleted_at.is_(None))
            .all()
        )
        if created_jobs:
            day_counter: Counter[str] = Counter()
            for (ts,) in created_jobs:
                if ts:
                    day_counter[ts.strftime("%A")] += 1
            if day_counter:
                busiest_day = day_counter.most_common(1)[0][0]
    except Exception:
        logging.getLogger(__name__).exception("get_tenant_metrics caught exception")
        pass

    avg_job_value = revenue_total / jobs_completed if jobs_completed > 0 else 0.0

    return TenantMetrics(
        tenant_id=tenant_id,
        period=period,
        jobs_created=jobs_created,
        jobs_completed=jobs_completed,
        revenue_total=round(revenue_total, 2),
        active_customers=active_customers,
        avg_job_value=round(avg_job_value, 2),
        top_technician=top_technician,
        busiest_day=busiest_day,
        computed_at=now,
    )


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/admin/metrics", tags=["metrics"])

_admin_dep = Depends(require_role("admin", "owner"))


@router.get("/{tenant_id}")
def get_metrics_for_tenant(
    tenant_id: str,
    period: str = "30d",
    control_db: Session = Depends(get_db),
    _: None = _admin_dep,
) -> dict:
    """Return usage metrics for a single tenant over the specified period."""
    if period not in _PERIOD_DAYS:
        raise HTTPException(status_code=422, detail=f"Invalid period. Use one of: {list(_PERIOD_DAYS)}")

    try:
        tid = uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid tenant_id format") from None

    tenant = control_db.query(Tenant).filter(Tenant.id == tid, Tenant.deleted_at.is_(None)).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tenant_db: Session | None = None
    try:
        tenant_db = _connect_tenant_db()
        metrics = get_tenant_metrics(tenant_id, period, tenant_db)
        result = asdict(metrics)
        result["computed_at"] = metrics.computed_at.isoformat()
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Metrics computation failed: {exc}") from exc
    finally:
        if tenant_db is not None:
            with contextlib.suppress(Exception):
                tenant_db.close()


@router.get("/summary")
def get_metrics_summary(
    control_db: Session = Depends(get_db),
    _: None = _admin_dep,
) -> list[dict]:
    """Return a brief metrics overview for all active tenants (30d window)."""
    tenants = control_db.query(Tenant).filter(Tenant.deleted_at.is_(None)).all()
    results: list[dict] = []

    for tenant in tenants:
        tenant_db: Session | None = None
        try:
            tenant_db = _connect_tenant_db()
            metrics = get_tenant_metrics(str(tenant.id), "30d", tenant_db)
            results.append(
                {
                    "tenant_id": metrics.tenant_id,
                    "period": metrics.period,
                    "jobs_created_30d": metrics.jobs_created,
                    "revenue_30d": metrics.revenue_total,
                    "active_customers_30d": metrics.active_customers,
                    "computed_at": metrics.computed_at.isoformat(),
                }
            )
        except Exception as exc:
            logger.warning("Metrics skipped for tenant %s: %s", tenant.id, exc)
        finally:
            if tenant_db is not None:
                with contextlib.suppress(Exception):
                    tenant_db.close()

    return results
