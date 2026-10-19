"""Variance Report — compare estimated vs actual materials used on jobs."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Job, JobCloseout, JobPartNeeded
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

# Actual parts used on a job (GDXA-1825) = the job's live closeout snapshot
# plus the job_parts_needed rows captured live from the truck. The
# van_inventory_log this report read before had no writer a user could reach.
#
# The closeout portion is read from the live JobCloseout.parts_used, not from
# source='closeout' checklist rows: a re-closeout keeps the BILLED closeout
# rows and adds the restated ones beside them, so summing those rows counts a
# corrected quantity twice, while the snapshot is the full re-attested list
# (core/closeouts.py). 'request' and 'vendor_invoice' rows are parts asked
# for or bought, not parts used.
_LIVE_CAPTURE_SOURCES = ("mobile", "van")
router = APIRouter(
    prefix="/api/variance",
    tags=["variance"],
    dependencies=[Depends(require_module("jobs"))],
)


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


@router.get("/job/{job_id}")
def job_variance(
    job_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compare estimate lines vs actual parts used (the job's capture rows)."""

    try:
        from uuid import UUID as _UUID
        _jid = _UUID(job_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Job not found") from None
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    job_obj = db.execute(
        select(Job).where(Job.id == _jid).where(Job.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not job_obj:
        raise HTTPException(status_code=404, detail="Job not found")
    job = {"id": str(job_obj.id), "description": job_obj.description, "status": job_obj.status}

    # Every estimate on the job, as the summary counts it. ORM, not raw SQL:
    # estimates.job_id is a Uuid (dashless hex on SQLite), and the raw query
    # selected el.total, a column estimate_lines does not have.
    estimated_items = [
        {
            "description": str(line.description),
            "quantity": int(line.quantity or 0),
            "unit_price": float(line.unit_price or 0),
            "total": float(line.line_total or 0),
        }
        for line in db.execute(
            select(EstimateLine)
            .join(Estimate, EstimateLine.estimate_id == Estimate.id)
            .where(Estimate.job_id == _jid)
            .order_by(EstimateLine.description)
        ).scalars().all()
    ]
    actual_items = _actual_items(db, [_jid]).get(_jid, [])

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
    """Jobs with biggest variances between estimated and actual material usage.

    Actual usage is the job's live closeout snapshot plus its mobile and van
    capture rows (see ``_actual_items``).
    """
    start_at = _day_start(start_date, "start_date")
    end_before = _day_start(end_date, "end_date")
    if end_before is not None:
        end_before += timedelta(days=1)  # end_date is inclusive of the whole day

    actual_by_job = {
        jid: sum(i["quantity_used"] for i in items)
        for jid, items in _actual_items(db).items()
    }
    job_ids = [jid for jid, qty in actual_by_job.items() if qty > 0]
    if not job_ids:
        return []

    job_query = select(Job).where(Job.id.in_(job_ids)).where(Job.deleted_at.is_(None))
    if start_at is not None:
        job_query = job_query.where(Job.created_at >= start_at)
    if end_before is not None:
        job_query = job_query.where(Job.created_at < end_before)
    jobs = db.execute(job_query).scalars().all()
    jobs = sorted(jobs, key=lambda j: actual_by_job[j.id], reverse=True)[:limit]
    if not jobs:
        return []

    est_by_job: dict[UUID, int] = {
        jid: int(qty or 0)
        for jid, qty in db.execute(
            select(Estimate.job_id, func.coalesce(func.sum(EstimateLine.quantity), 0))
            .join(EstimateLine, EstimateLine.estimate_id == Estimate.id)
            .where(Estimate.job_id.in_([j.id for j in jobs]))
            .group_by(Estimate.job_id)
        ).all()
    }

    results: list[dict[str, Any]] = []
    for job in jobs:
        est_qty = est_by_job.get(job.id, 0)
        actual_qty = actual_by_job[job.id]
        results.append({
            "job_id": str(job.id),
            "description": job.description,
            "status": job.status,
            "created_at": str(job.created_at) if job.created_at else None,
            "estimated_qty": est_qty,
            "actual_qty": actual_qty,
            "variance": actual_qty - est_qty,
            "variance_pct": round((actual_qty - est_qty) / max(est_qty, 1) * 100, 1),
        })

    # Sort by absolute variance descending
    results.sort(key=lambda x: abs(x["variance"]), reverse=True)
    return results


def _day_start(value: str | None, name: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.combine(date.fromisoformat(value), time.min, tzinfo=UTC)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} must be YYYY-MM-DD") from None


def _actual_items(db: Session, job_ids: list[UUID] | None = None) -> dict[UUID, list[dict[str, Any]]]:
    """Parts actually used, per job: every job, or only ``job_ids``.

    Capture rows are grouped in Python, not joined in SQL:
    ``job_parts_needed.job_id`` is a string stored as given, while ``jobs.id``
    is a Uuid that SQLite keeps as dashless hex, so a SQL equality join
    matches on one dialect only.
    """
    items: dict[UUID, list[dict[str, Any]]] = defaultdict(list)

    closeouts = select(JobCloseout).where(
        JobCloseout.superseded_at.is_(None), JobCloseout.deleted_at.is_(None)
    )
    if job_ids is not None:
        closeouts = closeouts.where(JobCloseout.job_id.in_(job_ids))
    for co in db.execute(closeouts).scalars().all():
        for part in co.parts_used or []:
            items[co.job_id].append({
                "name": str(part.get("name") or part.get("sku") or "Part"),
                "sku": part.get("sku"),
                "quantity_used": int(part.get("qty") or 0),
                "source": "closeout",
                "reason": part.get("note"),
                "used_at": str(co.closed_at) if co.closed_at else None,
            })

    rows = select(JobPartNeeded).where(JobPartNeeded.source.in_(_LIVE_CAPTURE_SOURCES))
    if job_ids is not None:
        keys = [k for jid in job_ids for k in (str(jid), jid.hex)]
        rows = rows.where(func.lower(JobPartNeeded.job_id).in_(keys))
    for row in db.execute(rows.order_by(JobPartNeeded.created_at)).scalars().all():
        try:
            jid = UUID(str(row.job_id))
        except ValueError:
            log.warning("variance_capture_row_bad_job_id id=%s job_id=%r", row.id, row.job_id)
            continue
        items[jid].append({
            "name": str(row.part_name),
            "sku": row.sku,
            "quantity_used": int(row.quantity or 0),
            "source": row.source,
            "reason": row.notes,
            "used_at": str(row.created_at) if row.created_at else None,
        })
    return items
