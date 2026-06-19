"""Variance Report — compare estimated vs actual materials used on jobs."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Job, VanInventoryItem, VanInventoryLog
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/variance",
    tags=["variance"],
    dependencies=[Depends(require_module("jobs"))],
)


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


@router.get("/job/{job_id}")
def job_variance(
    job_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compare estimate lines vs actual parts used (from van_inventory_log) for a job."""
    tid = _tid(request)

    try:
        from uuid import UUID as _UUID
        _jid = _UUID(job_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Job not found") from None
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    job_obj = db.execute(
        select(Job).where(Job.id == _jid)
    ).scalar_one_or_none()
    if not job_obj:
        raise HTTPException(status_code=404, detail="Job not found")
    job = {"id": str(job_obj.id), "description": job_obj.description, "status": job_obj.status}

    # Get estimate lines for this job (from estimate_lines or similar)
    estimated_items: list[dict[str, Any]] = []
    try:
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        est_rows = db.execute(
            text("""
                SELECT el.description, el.quantity, el.unit_price, el.total
                FROM estimate_lines el
                JOIN estimates e ON el.estimate_id = e.id
                WHERE e.job_id = :job_id
                ORDER BY el.description
            """),
            {"job_id": job_id},
        ).mappings().all()
        estimated_items = [
            {
                "description": str(r["description"]),
                "quantity": int(r["quantity"] or 0),
                "unit_price": float(r["unit_price"] or 0),
                "total": float(r["total"] or 0),
            }
            for r in est_rows
        ]
    except Exception:
        log.debug("estimate_lines table may not exist, skipping estimated items")

    # Get actual parts used from van_inventory_log via ORM
    actual_items: list[dict[str, Any]] = []
    try:
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        log_rows = db.execute(
            select(VanInventoryLog, VanInventoryItem)
            .join(VanInventoryItem, VanInventoryLog.van_inventory_id == VanInventoryItem.id)
            .where(VanInventoryLog.job_id == job_id)
            .order_by(VanInventoryLog.created_at)
        ).all()
        actual_items = [
            {
                "name": str(vil_item.name),
                "sku": vil_item.sku,
                "quantity_used": abs(int(vil_log.quantity_change)),
                "reason": vil_log.reason,
                "used_at": str(vil_log.created_at) if vil_log.created_at else None,
            }
            for vil_log, vil_item in log_rows
        ]
    except Exception:
        log.debug("van_inventory_log table may not exist, skipping actual items")

    estimated_total = sum(i["total"] for i in estimated_items)
    actual_total_qty = sum(i["quantity_used"] for i in actual_items)
    estimated_total_qty = sum(i["quantity"] for i in estimated_items)

    return {
        "job_id": job_id,
        "job_description": job["description"],
        "job_status": job["status"],
        "estimated_items": estimated_items,
        "actual_items": actual_items,
        "estimated_total_value": estimated_total,
        "estimated_total_qty": estimated_total_qty,
        "actual_total_qty": actual_total_qty,
        "quantity_variance": actual_total_qty - estimated_total_qty,
    }


@router.get("/summary")
def variance_summary(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    start_date: str | None = Query(None, description="ISO date YYYY-MM-DD"),
    end_date: str | None = Query(None, description="ISO date YYYY-MM-DD"),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Jobs with biggest variances between estimated and actual material usage."""
    tid = _tid(request)

    # Get jobs that have van_inventory_log entries
    params: dict[str, Any] = {"tid": tid, "limit": limit}
    date_filter = ""
    if start_date:
        date_filter += " AND j.created_at >= :start_date"
        params["start_date"] = start_date
    if end_date:
        date_filter += " AND j.created_at <= :end_date"
        params["end_date"] = end_date

    results: list[dict[str, Any]] = []
    try:
        rows = db.execute(
            text(f"""
                SELECT j.id AS job_id, j.description, j.status, j.created_at,
                       COALESCE(SUM(ABS(vil.quantity_change)), 0) AS actual_qty
                FROM jobs j
                LEFT JOIN van_inventory vi ON vi.company_id = j.company_id
                LEFT JOIN van_inventory_log vil ON vil.van_inventory_id = vi.id AND vil.job_id = CAST(j.id AS text)
                WHERE j.company_id = :tid {date_filter}
                GROUP BY j.id, j.description, j.status, j.created_at
                HAVING COALESCE(SUM(ABS(vil.quantity_change)), 0) > 0
                ORDER BY actual_qty DESC
                LIMIT :limit
            """),
            params,
        ).mappings().all()

        for r in rows:
            job_id = str(r["job_id"])
            # Get estimated quantity for this job
            est_qty = 0
            try:
                est_row = db.execute(
                    text("""
                        SELECT COALESCE(SUM(el.quantity), 0) AS est_qty
                        FROM estimate_lines el
                        JOIN estimates e ON el.estimate_id = e.id
                        WHERE e.job_id = :job_id AND e.company_id = :tid
                    """),
                    {"job_id": job_id, "tid": tid},
                ).mappings().first()
                if est_row:
                    est_qty = int(est_row["est_qty"] or 0)
            except Exception:
                logging.getLogger(__name__).exception("variance_summary caught exception")
                pass

            actual_qty = int(r["actual_qty"] or 0)
            results.append({
                "job_id": job_id,
                "description": r["description"],
                "status": r["status"],
                "created_at": str(r["created_at"]) if r["created_at"] else None,
                "estimated_qty": est_qty,
                "actual_qty": actual_qty,
                "variance": actual_qty - est_qty,
                "variance_pct": round((actual_qty - est_qty) / max(est_qty, 1) * 100, 1),
            })

        # Sort by absolute variance descending
        results.sort(key=lambda x: abs(x["variance"]), reverse=True)
    except Exception:
        log.exception("variance_summary_query_failed")

    return results
