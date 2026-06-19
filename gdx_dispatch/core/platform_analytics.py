"""gdx_dispatch/core/platform_analytics.py — Cross-tenant platform analytics (admin only).

Aggregates anonymized metrics across all tenant DBs and caches in Redis.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from fastapi import APIRouter, Depends, Query, Request
from redis import Redis, from_url
from sqlalchemy import create_engine, func, text
from sqlalchemy.orm import Session, sessionmaker

from gdx_dispatch.control.models import Tenant, TenantModuleGrant
from gdx_dispatch.core.database import SessionLocal, SessionLocal, get_db
from gdx_dispatch.core.health_score import TenantHealthLog
from gdx_dispatch.core.modules import require_role
from gdx_dispatch.models.tenant_models import Invoice, Job

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclass — result carrier
# ---------------------------------------------------------------------------

@dataclass
class PlatformMetrics:
    period: str
    total_tenants: int
    active_tenants: int
    total_jobs: int
    total_revenue_sum: float
    avg_revenue_per_tenant: float
    top_modules_by_adoption: list  # [{module, tenant_count, pct_of_total}]
    churn_risk_count: int
    new_tenants_this_period: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _redis() -> Redis:
    return from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)


def _parse_period(period: str) -> timedelta:
    """Parse period string like '7d', '30d', '90d' into a timedelta."""
    _map = {"7d": timedelta(days=7), "30d": timedelta(days=30), "90d": timedelta(days=90)}
    return _map.get(period, timedelta(days=30))


def _open_tenant_session() -> Session:
    return SessionLocal()


# ---------------------------------------------------------------------------
# Core aggregation
# ---------------------------------------------------------------------------

def get_platform_metrics(period: str = "30d", control_db: Session | None = None) -> PlatformMetrics:
    """Aggregate platform-wide metrics across all tenant DBs.

    Results are cached in Redis for 1 hour.
    """
    cache_key = f"platform:metrics:{period}"
    try:
        cached = _redis().get(cache_key)
        if cached:
            data = json.loads(cached)
            return PlatformMetrics(**data)
    except Exception as exc:
        logger.warning("Redis cache read failed: %s", exc)

    # Use caller-supplied session or open a fresh one
    _own_db = False
    if control_db is None:
        control_db = SessionLocal()
        _own_db = True

    try:
        delta = _parse_period(period)
        now = datetime.now(timezone.utc)
        cutoff = now - delta

        # --- tenant list ---
        tenants = control_db.query(Tenant).filter(Tenant.deleted_at.is_(None)).all()
        total_tenants = len(tenants)
        new_tenants_this_period = sum(
            1 for t in tenants
            if t.created_at and (
                t.created_at.replace(tzinfo=timezone.utc) if t.created_at.tzinfo is None else t.created_at
            ) >= cutoff
        )

        # --- per-tenant aggregation ---
        total_jobs = 0
        total_revenue_sum = 0.0
        active_tenant_ids: set[str] = set()

        for tenant in tenants:
            tenant_db: Session | None = None
            try:
                tenant_db = _open_tenant_session()

                jobs_count = (
                    tenant_db.query(Job)
                    .filter(Job.created_at >= cutoff, Job.deleted_at.is_(None))
                    .count()
                )
                if jobs_count > 0:
                    active_tenant_ids.add(str(tenant.id))
                    total_jobs += jobs_count

                rev = (
                    tenant_db.query(func.coalesce(func.sum(Invoice.total), 0.0))
                    .filter(Invoice.created_at >= cutoff, Invoice.deleted_at.is_(None))
                    .scalar()
                ) or 0.0
                total_revenue_sum += float(rev)

            except Exception as exc:
                logger.warning("Skipping tenant %s in platform metrics: %s", tenant.id, exc)
            finally:
                if tenant_db is not None:
                    with contextlib.suppress(Exception):
                        tenant_db.close()

        active_tenants = len(active_tenant_ids)
        avg_revenue_per_tenant = total_revenue_sum / max(active_tenants, 1)

        # --- churn risk: latest health score < 40 per tenant ---
        churn_risk_count = 0
        try:
            latest_subq = (
                control_db.query(
                    TenantHealthLog.tenant_id,
                    func.max(TenantHealthLog.computed_at).label("max_ts"),
                )
                .group_by(TenantHealthLog.tenant_id)
                .subquery()
            )
            from sqlalchemy import and_
            churn_risk_count = (
                control_db.query(TenantHealthLog)
                .join(
                    latest_subq,
                    and_(
                        TenantHealthLog.tenant_id == latest_subq.c.tenant_id,
                        TenantHealthLog.computed_at == latest_subq.c.max_ts,
                    ),
                )
                .filter(TenantHealthLog.score < 40)
                .count()
            )
        except Exception as exc:
            logger.warning("Churn risk count failed: %s", exc)

        # --- module adoption ---
        top_modules_by_adoption: list[dict] = []
        try:
            rows = (
                control_db.query(
                    TenantModuleGrant.module_key,
                    func.count(func.distinct(TenantModuleGrant.tenant_id)).label("cnt"),
                )
                .group_by(TenantModuleGrant.module_key)
                .order_by(func.count(func.distinct(TenantModuleGrant.tenant_id)).desc())
                .all()
            )
            denom = max(total_tenants, 1)
            top_modules_by_adoption = [
                {
                    "module": row.module_key,
                    "tenant_count": row.cnt,
                    "pct_of_total": round(row.cnt / denom * 100, 1),
                }
                for row in rows
            ]
        except Exception as exc:
            logger.warning("Module adoption query failed: %s", exc)

        metrics = PlatformMetrics(
            period=period,
            total_tenants=total_tenants,
            active_tenants=active_tenants,
            total_jobs=total_jobs,
            total_revenue_sum=round(total_revenue_sum, 2),
            avg_revenue_per_tenant=round(avg_revenue_per_tenant, 2),
            top_modules_by_adoption=top_modules_by_adoption,
            churn_risk_count=churn_risk_count,
            new_tenants_this_period=new_tenants_this_period,
        )

        # --- cache result ---
        try:
            _redis().setex(cache_key, 3600, json.dumps(asdict(metrics)))
        except Exception as exc:
            logger.warning("Redis cache write failed: %s", exc)

        return metrics

    finally:
        if _own_db:
            with contextlib.suppress(Exception):
                control_db.close()


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/platform", tags=["platform-analytics"])
_admin_dep = Depends(require_role("admin", "owner"))


@router.get("/metrics")
def platform_metrics_endpoint(
    period: str = Query(default="30d", pattern="^(7d|30d|90d)$"),
    control_db: Session = Depends(get_db),
    _: None = _admin_dep,
) -> dict:
    """Aggregate platform metrics across all tenant DBs (cached 1h)."""
    metrics = get_platform_metrics(period=period, control_db=control_db)
    return asdict(metrics)


@router.get("/growth")
def platform_growth_endpoint(
    control_db: Session = Depends(get_db),
    _: None = _admin_dep,
) -> dict:
    """New tenant signups and churn signals grouped by month (last 12 months)."""
    cache_key = "platform:growth"
    try:
        cached = _redis().get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as exc:
        logger.warning("Redis cache read failed (growth): %s", exc)

    result: dict = {"new_tenants": [], "churn": []}

    try:
        now = datetime.now(timezone.utc)
        cutoff_12m = now - timedelta(days=365)

        # New tenants grouped by month
        new_rows = (
            control_db.query(
                func.to_char(Tenant.created_at, "YYYY-MM").label("month"),
                func.count(Tenant.id).label("count"),
            )
            .filter(
                Tenant.deleted_at.is_(None),
                Tenant.created_at >= cutoff_12m,
            )
            .group_by(func.to_char(Tenant.created_at, "YYYY-MM"))
            .order_by(func.to_char(Tenant.created_at, "YYYY-MM"))
            .all()
        )
        result["new_tenants"] = [{"month": r.month, "count": r.count} for r in new_rows]
    except Exception:
        # SQLite fallback (tests)
        try:
            new_rows = control_db.execute(
                text(
                    "SELECT strftime('%Y-%m', created_at) AS month, COUNT(*) AS count "
                    "FROM tenants WHERE deleted_at IS NULL "
                    "AND created_at >= :cutoff GROUP BY month ORDER BY month"
                ),
                {"cutoff": (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()},
            ).fetchall()
            result["new_tenants"] = [{"month": r[0], "count": r[1]} for r in new_rows if r[0]]
        except Exception as exc:
            logger.warning("Growth new_tenants query failed: %s", exc)

    try:
        now = datetime.now(timezone.utc)
        cutoff_12m = now - timedelta(days=365)

        churn_rows = (
            control_db.query(
                func.to_char(TenantHealthLog.computed_at, "YYYY-MM").label("month"),
                func.count(func.distinct(TenantHealthLog.tenant_id)).label("count"),
            )
            .filter(
                TenantHealthLog.score < 40,
                TenantHealthLog.computed_at >= cutoff_12m,
            )
            .group_by(func.to_char(TenantHealthLog.computed_at, "YYYY-MM"))
            .order_by(func.to_char(TenantHealthLog.computed_at, "YYYY-MM"))
            .all()
        )
        result["churn"] = [{"month": r.month, "count": r.count} for r in churn_rows]
    except Exception:
        try:
            churn_rows = control_db.execute(
                text(
                    "SELECT strftime('%Y-%m', computed_at) AS month, "
                    "COUNT(DISTINCT tenant_id) AS count "
                    "FROM tenant_health_logs WHERE score < 40 "
                    "AND computed_at >= :cutoff GROUP BY month ORDER BY month"
                ),
                {"cutoff": (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()},
            ).fetchall()
            result["churn"] = [{"month": r[0], "count": r[1]} for r in churn_rows if r[0]]
        except Exception as exc:
            logger.warning("Growth churn query failed: %s", exc)

    try:
        _redis().setex(cache_key, 3600, json.dumps(result))
    except Exception as exc:
        logger.warning("Redis cache write failed (growth): %s", exc)

    return result


@router.get("/module-adoption")
def platform_module_adoption_endpoint(
    control_db: Session = Depends(get_db),
    _: None = _admin_dep,
) -> list:
    """Module adoption rates across all tenants (cached 1h)."""
    cache_key = "platform:module-adoption"
    try:
        cached = _redis().get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as exc:
        logger.warning("Redis cache read failed (module-adoption): %s", exc)

    result: list = []
    try:
        total_tenants = control_db.query(Tenant).filter(Tenant.deleted_at.is_(None)).count()
        rows = (
            control_db.query(
                TenantModuleGrant.module_key,
                func.count(func.distinct(TenantModuleGrant.tenant_id)).label("cnt"),
            )
            .group_by(TenantModuleGrant.module_key)
            .order_by(func.count(func.distinct(TenantModuleGrant.tenant_id)).desc())
            .all()
        )
        denom = max(total_tenants, 1)
        result = [
            {
                "module": row.module_key,
                "tenant_count": row.cnt,
                "pct_of_total": round(row.cnt / denom * 100, 1),
            }
            for row in rows
        ]
    except Exception as exc:
        logger.warning("Module adoption endpoint query failed: %s", exc)

    try:
        _redis().setex(cache_key, 3600, json.dumps(result))
    except Exception as exc:
        logger.warning("Redis cache write failed (module-adoption): %s", exc)

    return result


# ---------------------------------------------------------------------------
# PlatformAnalytics class — tenant KPI and cohort helpers
# ---------------------------------------------------------------------------

import uuid as _uuid_mod  # noqa: E402 — local alias to avoid shadowing


class PlatformAnalytics:
    """High-level analytics helpers for tenant KPIs, cohort analysis, and feature adoption."""

    # ------------------------------------------------------------------
    # Tenant KPIs
    # ------------------------------------------------------------------

    def get_tenant_kpis(
        self,
        tenant_id: str,
        period: str,
        tenant_db: Session,
    ) -> dict:
        """Return per-tenant KPI dict for the given period.

        Keys: total_jobs, revenue, avg_job_value, close_rate,
              customer_satisfaction, technician_utilization
        """
        delta = _parse_period(period)
        now = datetime.now(timezone.utc)
        cutoff = now - delta

        total_jobs = 0
        revenue = 0.0
        completed_jobs = 0
        jobs_with_tech = 0

        try:
            total_jobs = (
                tenant_db.query(Job)
                .filter(Job.created_at >= cutoff, Job.deleted_at.is_(None))
                .count()
            )
        except Exception as exc:
            logger.warning("get_tenant_kpis total_jobs failed for %s: %s", tenant_id, exc)

        try:
            completed_jobs = (
                tenant_db.query(Job)
                .filter(
                    Job.lifecycle_stage == "completed",
                    Job.completed_at >= cutoff,
                    Job.deleted_at.is_(None),
                )
                .count()
            )
        except Exception as exc:
            logger.warning("get_tenant_kpis completed_jobs failed for %s: %s", tenant_id, exc)

        try:
            rev_result = (
                tenant_db.query(func.coalesce(func.sum(Invoice.total), 0.0))
                .filter(Invoice.created_at >= cutoff, Invoice.deleted_at.is_(None))
                .scalar()
            )
            revenue = float(rev_result or 0.0)
        except Exception as exc:
            logger.warning("get_tenant_kpis revenue failed for %s: %s", tenant_id, exc)

        try:
            jobs_with_tech = (
                tenant_db.query(Job)
                .filter(
                    Job.created_at >= cutoff,
                    Job.deleted_at.is_(None),
                    Job.assigned_to.isnot(None),
                )
                .count()
            )
        except Exception as exc:
            logger.warning("get_tenant_kpis jobs_with_tech failed for %s: %s", tenant_id, exc)

        denom = max(total_jobs, 1)
        avg_job_value = revenue / max(completed_jobs, 1) if completed_jobs > 0 else 0.0
        close_rate = round(completed_jobs / denom * 100, 1)
        technician_utilization = round(jobs_with_tech / denom * 100, 1)

        return {
            "tenant_id": tenant_id,
            "period": period,
            "total_jobs": total_jobs,
            "revenue": round(revenue, 2),
            "avg_job_value": round(avg_job_value, 2),
            "close_rate": close_rate,
            "customer_satisfaction": 0.0,  # placeholder — requires review data
            "technician_utilization": technician_utilization,
        }

    # ------------------------------------------------------------------
    # Platform-wide summary (admin)
    # ------------------------------------------------------------------

    def get_platform_metrics_summary(self, control_db: Session) -> dict:
        """Admin-only: aggregate platform health (total_tenants, MRR estimate, churn_rate, avg_jobs_per_tenant)."""
        try:
            pm = get_platform_metrics(period="30d", control_db=control_db)
            churn_rate = round(pm.churn_risk_count / max(pm.total_tenants, 1) * 100, 1)
            avg_jobs = round(pm.total_jobs / max(pm.active_tenants, 1), 1)
            return {
                "total_tenants": pm.total_tenants,
                "active_tenants": pm.active_tenants,
                "mrr": pm.avg_revenue_per_tenant,  # approximate: avg monthly revenue per tenant
                "churn_rate": churn_rate,
                "churn_risk_count": pm.churn_risk_count,
                "avg_jobs_per_tenant": avg_jobs,
                "new_tenants_this_period": pm.new_tenants_this_period,
            }
        except Exception as exc:
            logger.warning("get_platform_metrics_summary failed: %s", exc)
            return {
                "total_tenants": 0,
                "active_tenants": 0,
                "mrr": 0.0,
                "churn_rate": 0.0,
                "churn_risk_count": 0,
                "avg_jobs_per_tenant": 0.0,
                "new_tenants_this_period": 0,
            }

    # ------------------------------------------------------------------
    # Cohort analysis
    # ------------------------------------------------------------------

    def get_cohort_analysis(self, month: str, control_db: Session) -> list:
        """Return tenant retention data for the signup cohort in the given YYYY-MM month.

        Returns: [{cohort_month, cohort_size, active_m1, active_m2, active_m3}]
        Cross-tenant job queries are skipped here (too expensive); active_mN fields are
        populated as 0 placeholders — a background job should materialise these.
        """
        result: list[dict] = []
        try:
            # Parse YYYY-MM into start/end boundaries
            year, mon = int(month[:4]), int(month[5:7])
            cohort_start = datetime(year, mon, 1, tzinfo=timezone.utc)
            # End of month = start of next month
            if mon == 12:
                cohort_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                cohort_end = datetime(year, mon + 1, 1, tzinfo=timezone.utc)

            tenants_in_cohort = (
                control_db.query(Tenant)
                .filter(
                    Tenant.deleted_at.is_(None),
                    Tenant.created_at >= cohort_start,
                    Tenant.created_at < cohort_end,
                )
                .all()
            )
            cohort_size = len(tenants_in_cohort)
            result.append(
                {
                    "cohort_month": month,
                    "cohort_size": cohort_size,
                    "active_m1": 0,  # placeholder — materialised by background job
                    "active_m2": 0,
                    "active_m3": 0,
                }
            )
        except Exception as exc:
            logger.warning("get_cohort_analysis failed for month %s: %s", month, exc)
            result = [{"cohort_month": month, "cohort_size": 0, "active_m1": 0, "active_m2": 0, "active_m3": 0}]

        return result

    # ------------------------------------------------------------------
    # Feature adoption
    # ------------------------------------------------------------------

    def get_feature_adoption(self, tenant_id: str, control_db: Session) -> dict:
        """Return module grant and usage summary for a tenant.

        Returns: {modules_granted, modules_used, usage_frequency}
        Note: TenantModuleGrant has no last_used_at column; modules_used defaults to
        modules_granted (all granted modules treated as potentially used).
        """
        modules_granted: list[str] = []
        try:
            tid = _uuid_mod.UUID(tenant_id)
            rows = (
                control_db.query(TenantModuleGrant.module_key)
                .filter(
                    TenantModuleGrant.tenant_id == tid,
                    TenantModuleGrant.expires_at.is_(None)
                    | (TenantModuleGrant.expires_at > datetime.now(timezone.utc)),
                )
                .all()
            )
            modules_granted = [r.module_key for r in rows]
        except Exception as exc:
            logger.warning("get_feature_adoption grants query failed for %s: %s", tenant_id, exc)

        # Without a last_used_at column, treat all granted modules as "used"
        modules_used = list(modules_granted)
        usage_frequency = {m: 1 for m in modules_used}

        return {
            "tenant_id": tenant_id,
            "modules_granted": modules_granted,
            "modules_used": modules_used,
            "usage_frequency": usage_frequency,
        }


# ---------------------------------------------------------------------------
# Additional API routes — tenant KPI, revenue trend, technician performance
# ---------------------------------------------------------------------------

from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

_templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "..", "templates")
)

_analytics = PlatformAnalytics()


@router.get("/kpis")
def analytics_kpis_endpoint(
    tenant_id: str = Query(..., description="UUID of the tenant"),
    period: str = Query(default="30d", pattern="^(7d|30d|90d)$"),
    control_db: Session = Depends(get_db),
    _: None = _admin_dep,
) -> dict:
    """Per-tenant KPI summary (cached 15 min)."""
    cache_key = f"analytics:kpis:{tenant_id}:{period}"
    try:
        cached = _redis().get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as exc:
        logger.warning("Redis cache read failed (kpis): %s", exc)

    try:
        tid = _uuid_mod.UUID(tenant_id)
    except ValueError:
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(status_code=422, detail="Invalid tenant_id format") from None

    from fastapi import HTTPException as _HTTPException
    tenant = control_db.query(Tenant).filter(Tenant.id == tid, Tenant.deleted_at.is_(None)).first()
    if not tenant:
        raise _HTTPException(status_code=404, detail="Tenant not found")

    tenant_db: Session | None = None
    try:
        tenant_db = _open_tenant_session()
        result = _analytics.get_tenant_kpis(tenant_id, period, tenant_db)
    except Exception as exc:
        logger.warning("KPI computation failed for %s: %s", tenant_id, exc)
        result = {
            "tenant_id": tenant_id, "period": period,
            "total_jobs": 0, "revenue": 0.0, "avg_job_value": 0.0,
            "close_rate": 0.0, "customer_satisfaction": 0.0, "technician_utilization": 0.0,
        }
    finally:
        if tenant_db is not None:
            with contextlib.suppress(Exception):
                tenant_db.close()

    try:
        _redis().setex(cache_key, 900, json.dumps(result))
    except Exception as exc:
        logger.warning("Redis cache write failed (kpis): %s", exc)

    return result


@router.get("/revenue-trend")
def analytics_revenue_trend_endpoint(
    tenant_id: str = Query(..., description="UUID of the tenant"),
    months: int = Query(default=12, ge=1, le=24),
    control_db: Session = Depends(get_db),
    _: None = _admin_dep,
) -> list:
    """Revenue time series for a tenant (last N months, cached 1h)."""
    cache_key = f"analytics:revenue-trend:{tenant_id}:{months}"
    try:
        cached = _redis().get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as exc:
        logger.warning("Redis cache read failed (revenue-trend): %s", exc)

    try:
        tid = _uuid_mod.UUID(tenant_id)
    except ValueError:
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(status_code=422, detail="Invalid tenant_id format") from None

    from fastapi import HTTPException as _HTTPException
    tenant = control_db.query(Tenant).filter(Tenant.id == tid, Tenant.deleted_at.is_(None)).first()
    if not tenant:
        raise _HTTPException(status_code=404, detail="Tenant not found")

    result: list[dict] = []
    tenant_db: Session | None = None
    try:
        tenant_db = _open_tenant_session()
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=months * 31)

        # Try PostgreSQL to_char first, fall back to SQLite strftime
        try:
            rows = (
                tenant_db.query(
                    func.to_char(Invoice.created_at, "YYYY-MM").label("month"),
                    func.coalesce(func.sum(Invoice.total), 0.0).label("revenue"),
                )
                .filter(Invoice.created_at >= cutoff, Invoice.deleted_at.is_(None))
                .group_by(func.to_char(Invoice.created_at, "YYYY-MM"))
                .order_by(func.to_char(Invoice.created_at, "YYYY-MM"))
                .all()
            )
            result = [{"month": r.month, "revenue": float(r.revenue)} for r in rows]
        except Exception:
            logging.getLogger(__name__).exception("analytics_revenue_trend_endpoint caught exception")
            rows_raw = tenant_db.execute(
                text(
                    "SELECT strftime('%Y-%m', created_at) AS month, "
                    "COALESCE(SUM(total), 0.0) AS revenue "
                    "FROM invoices WHERE deleted_at IS NULL AND created_at >= :cutoff "
                    "GROUP BY month ORDER BY month"
                ),
                {"cutoff": cutoff.isoformat()},
            ).fetchall()
            result = [{"month": r[0], "revenue": float(r[1])} for r in rows_raw if r[0]]

    except Exception as exc:
        logger.warning("Revenue trend query failed for %s: %s", tenant_id, exc)
    finally:
        if tenant_db is not None:
            with contextlib.suppress(Exception):
                tenant_db.close()

    try:
        _redis().setex(cache_key, 3600, json.dumps(result))
    except Exception as exc:
        logger.warning("Redis cache write failed (revenue-trend): %s", exc)

    return result


@router.get("/technician-performance")
def analytics_technician_performance_endpoint(
    tenant_id: str = Query(..., description="UUID of the tenant"),
    period: str = Query(default="30d", pattern="^(7d|30d|90d)$"),
    control_db: Session = Depends(get_db),
    _: None = _admin_dep,
) -> list:
    """Per-technician performance stats for a tenant (cached 15 min)."""
    cache_key = f"analytics:tech-perf:{tenant_id}:{period}"
    try:
        cached = _redis().get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as exc:
        logger.warning("Redis cache read failed (tech-perf): %s", exc)

    try:
        tid = _uuid_mod.UUID(tenant_id)
    except ValueError:
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(status_code=422, detail="Invalid tenant_id format") from None

    from fastapi import HTTPException as _HTTPException
    tenant = control_db.query(Tenant).filter(Tenant.id == tid, Tenant.deleted_at.is_(None)).first()
    if not tenant:
        raise _HTTPException(status_code=404, detail="Tenant not found")

    result: list[dict] = []
    tenant_db: Session | None = None
    try:
        tenant_db = _open_tenant_session()
        delta = _parse_period(period)
        cutoff = datetime.now(timezone.utc) - delta

        # Group completed jobs by assigned_to technician
        rows = (
            tenant_db.query(
                Job.assigned_to,
                func.count(Job.id).label("jobs_completed"),
            )
            .filter(
                Job.lifecycle_stage == "completed",
                Job.completed_at >= cutoff,
                Job.deleted_at.is_(None),
                Job.assigned_to.isnot(None),
            )
            .group_by(Job.assigned_to)
            .order_by(func.count(Job.id).desc())
            .all()
        )

        # Total jobs in period for utilization denominator
        total_in_period = (
            tenant_db.query(Job)
            .filter(Job.created_at >= cutoff, Job.deleted_at.is_(None))
            .count()
        ) or 1

        for row in rows:
            tech = row.assigned_to
            completed = row.jobs_completed

            # Revenue for this technician's completed jobs (via invoice on job)
            try:
                tech_revenue = (
                    tenant_db.query(func.coalesce(func.sum(Invoice.total), 0.0))
                    .join(Job, Invoice.job_id == Job.id)
                    .filter(
                        Job.assigned_to == tech,
                        Job.lifecycle_stage == "completed",
                        Job.completed_at >= cutoff,
                        Job.deleted_at.is_(None),
                        Invoice.deleted_at.is_(None),
                    )
                    .scalar()
                ) or 0.0
                tech_revenue = float(tech_revenue)
            except Exception:
                logging.getLogger(__name__).exception("analytics_technician_performance_endpoint caught exception")
                tech_revenue = 0.0

            avg_job_value = round(tech_revenue / max(completed, 1), 2)
            utilization_pct = round(completed / total_in_period * 100, 1)

            result.append(
                {
                    "technician": tech,
                    "jobs_completed": completed,
                    "avg_job_value": avg_job_value,
                    "utilization_pct": utilization_pct,
                }
            )

    except Exception as exc:
        logger.warning("Technician performance query failed for %s: %s", tenant_id, exc)
    finally:
        if tenant_db is not None:
            with contextlib.suppress(Exception):
                tenant_db.close()

    try:
        _redis().setex(cache_key, 900, json.dumps(result))
    except Exception as exc:
        logger.warning("Redis cache write failed (tech-perf): %s", exc)

    return result


# ---------------------------------------------------------------------------
# Analytics dashboard UI page  (GET /analytics)
# ---------------------------------------------------------------------------

analytics_page_router = APIRouter(tags=["analytics-ui"])


@analytics_page_router.get("/analytics", response_class=HTMLResponse)
def analytics_dashboard_page(
    request: Request,  # noqa: F821 — Request imported below
) -> HTMLResponse:
    """Render the tenant analytics dashboard HTML page."""
    from fastapi import Request as _Request  # noqa: F401

    current_user = getattr(request.state, "current_user", None)
    if not current_user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/auth/login", status_code=302)

    return _templates.TemplateResponse(
        "analytics.html",
        {
            "request": request,
            "current_user": current_user,
            "page_title": "Analytics",
            "flash_messages": getattr(request.state, "flash_messages", []),
        },
    )
