"""Performance — user performance stats aggregated from existing tables."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import User
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/performance",
    tags=["performance"],
    dependencies=[Depends(require_module("jobs"))],
)


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


def _safe_int(val: Any) -> int:
    try:
        return int(val or 0)
    except (TypeError, ValueError):  # silent failure for type conversion utility
        logging.getLogger(__name__).exception("_safe_int caught exception")
        return 0


def _safe_float(val: Any) -> float:
    try:
        return round(float(val or 0), 2)
    except (TypeError, ValueError):  # silent failure for invalid numeric input
        logging.getLogger(__name__).exception("_safe_float caught exception")
        return 0.0


def _build_user_stats(db: Session, tid: str, user_id: str, period: str | None) -> dict[str, Any]:
    """Aggregate stats for a single user from existing tables."""
    stats: dict[str, Any] = {
        "jobs_completed": 0,
        "revenue": 0.0,
        "avg_job_value": 0.0,
        "estimates_created": 0,
        "estimates_accepted": 0,
        "hours_worked": 0.0,
        "tasks_completed": 0,
        "commission_earned": 0.0,
        "safety_checklists": 0,
    }

    period_filter = ""
    params: dict[str, Any] = {"tid": tid, "user_id": user_id}
    if period:
        period_filter = " AND created_at >= :period_start AND created_at < :period_end"
        params["period_start"] = f"{period}-01"
        # Approximate end: add 32 days, truncate
        try:
            year, month = int(period[:4]), int(period[5:7])
            end = f"{year + 1}-01-01" if month == 12 else f"{year}-{month + 1:02d}-01"
            params["period_end"] = end
        except (ValueError, IndexError):
            logging.getLogger(__name__).exception("_build_user_stats caught exception")
            period_filter = ""

    # Jobs completed
    try:
        row = db.execute(
            text(f"""
                SELECT COUNT(*) AS cnt FROM jobs
                WHERE company_id = :tid AND assigned_to = :user_id
                  AND status IN ('Complete', 'Completed', 'complete', 'completed')
                  {period_filter}
            """),
            params,
        ).mappings().first()
        stats["jobs_completed"] = _safe_int(row["cnt"]) if row else 0
    except Exception:
        log.debug("jobs table query failed for user stats")

    # Revenue from invoices
    try:
        row = db.execute(
            text(f"""
                SELECT COALESCE(SUM(i.total), 0) AS revenue
                FROM invoices i
                JOIN jobs j ON i.job_id = j.id
                WHERE j.company_id = :tid AND j.assigned_to = :user_id
                  {period_filter.replace('created_at', 'i.created_at')}
            """),
            params,
        ).mappings().first()
        revenue = _safe_float(row["revenue"]) if row else 0.0
        stats["revenue"] = revenue
        if stats["jobs_completed"] > 0:
            stats["avg_job_value"] = round(revenue / stats["jobs_completed"], 2)
    except Exception:
        log.debug("invoices table query failed for user stats")

    # Estimates created / accepted
    try:
        row = db.execute(
            text(f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status IN ('accepted', 'approved') THEN 1 ELSE 0 END) AS accepted
                FROM estimates
                WHERE company_id = :tid AND created_by = :user_id
                  {period_filter}
            """),
            params,
        ).mappings().first()
        if row:
            stats["estimates_created"] = _safe_int(row["total"])
            stats["estimates_accepted"] = _safe_int(row["accepted"])
    except Exception:
        log.debug("estimates table query failed for user stats")

    # Timeclock hours
    try:
        row = db.execute(
            text(f"""
                SELECT COALESCE(SUM(hours_worked), 0) AS total_hours
                FROM timeclock_entries
                WHERE company_id = :tid AND user_id = :user_id
                  {period_filter.replace('created_at', 'clock_in')}
            """),
            params,
        ).mappings().first()
        stats["hours_worked"] = _safe_float(row["total_hours"]) if row else 0.0
    except Exception:
        log.debug("timeclock_entries table query failed for user stats")

    # Tasks completed
    try:
        row = db.execute(
            text(f"""
                SELECT COUNT(*) AS cnt FROM planner_tasks
                WHERE company_id = :tid AND assigned_to = :user_id
                  AND status = 'done'
                  {period_filter}
            """),
            params,
        ).mappings().first()
        stats["tasks_completed"] = _safe_int(row["cnt"]) if row else 0
    except Exception:
        log.debug("planner_tasks table query failed for user stats")

    # Commission earned
    try:
        dict(params)
        if period:
            row = db.execute(
                text("""
                    SELECT COALESCE(SUM(total), 0) AS earned FROM commission_entries
                    WHERE company_id = :tid AND user_id = :user_id AND period = :period
                """),
                {"tid": tid, "user_id": user_id, "period": period},
            ).mappings().first()
        else:
            row = db.execute(
                text("""
                    SELECT COALESCE(SUM(total), 0) AS earned FROM commission_entries
                    WHERE company_id = :tid AND user_id = :user_id
                """),
                {"tid": tid, "user_id": user_id},
            ).mappings().first()
        stats["commission_earned"] = _safe_float(row["earned"]) if row else 0.0
    except Exception:
        log.debug("commission_entries table query failed for user stats")

    # Safety checklists completed
    try:
        row = db.execute(
            text(f"""
                SELECT COUNT(*) AS cnt FROM safety_checklists
                WHERE company_id = :tid AND technician_id = :user_id
                  AND completed = true AND deleted_at IS NULL
                  {period_filter}
            """),
            params,
        ).mappings().first()
        stats["safety_checklists"] = _safe_int(row["cnt"]) if row else 0
    except Exception:
        log.debug("safety_checklists table query failed for user stats")

    return stats


@router.get("/users")
def all_users_performance(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    period: str | None = Query(None, description="YYYY-MM format"),
) -> dict[str, Any]:
    """All users with stats for a period."""
    tid = _tid(request)

    # Get all users for this tenant — ORM
    users: list[dict[str, Any]] = []
    try:
        user_rows = db.execute(
            select(
                User.id,
                User.name,
                User.full_name,
                User.email,
                User.role,
            )
            .where(
                User.company_id == tid,
                User.deleted_at.is_(None),
            )
            .order_by(User.name)
        ).mappings().all()
        users = [dict(r) for r in user_rows]
    except Exception:
        log.debug("users ORM query failed")

    result = []
    for u in users:
        uid = str(u["id"])
        stats = _build_user_stats(db, tid, uid, period)
        result.append({
            "id": uid,
            "name": u.get("name") or u.get("full_name") or "",
            "email": u.get("email") or "",
            "role": u.get("role") or "",
            "stats": stats,
        })

    return {"users": result, "period": period}


@router.get("/users/{user_id}")
def user_performance(
    user_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    period: str | None = Query(None, description="YYYY-MM format"),
) -> dict[str, Any]:
    """Single user detail with stats."""
    tid = _tid(request)

    # Verify user belongs to tenant — ORM
    u = None
    try:
        u = db.execute(
            select(User.id, User.name, User.full_name, User.email, User.role)
            .where(User.id == user_id, User.company_id == tid)
        ).mappings().first()
    except Exception:
        log.debug("user lookup failed")

    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    stats = _build_user_stats(db, tid, user_id, period)
    return {
        "id": str(u["id"]),
        "name": u.get("name") or u.get("full_name") or "",
        "email": u.get("email") or "",
        "role": u.get("role") or "",
        "stats": stats,
        "period": period,
    }
