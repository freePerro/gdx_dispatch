from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Invoice, Job
from gdx_dispatch.modules.timeclock.models import TimeClock


def job_costing_report(start_date: date, end_date: date, db: Session) -> list[dict]:
    """Query Jobs + Invoices, return list of dicts with job_id, title, total_billed, labor_minutes
    for jobs completed in the given date range."""
    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
    end_dt = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc)

    stmt = (
        select(
            Job.id,
            Job.title,
            func.coalesce(func.sum(Invoice.total), 0).label("total_billed"),
        )
        .outerjoin(Invoice, Invoice.job_id == Job.id)
        .where(
            Job.completed_at >= start_dt,
            Job.completed_at <= end_dt,
            Job.deleted_at.is_(None),
        )
        .group_by(Job.id, Job.title)
    )

    rows = db.execute(stmt).all()

    # Fetch labor_minutes per job from timeclock
    labor_stmt = (
        select(
            TimeClock.job_id,
            func.coalesce(func.sum(TimeClock.labor_minutes), 0).label("total_labor_minutes"),
        )
        .where(TimeClock.job_id.isnot(None))
        .group_by(TimeClock.job_id)
    )
    labor_rows = {str(r.job_id): int(r.total_labor_minutes) for r in db.execute(labor_stmt).all()}

    result = []
    for row in rows:
        result.append(
            {
                "job_id": str(row.id),
                "title": row.title,
                "total_billed": float(row.total_billed),
                "labor_minutes": labor_rows.get(str(row.id), 0),
            }
        )
    return result


def tech_performance_report(start_date: date, end_date: date, db: Session) -> list[dict]:
    """Query TimeClock, group by technician_id, return [{technician_id, total_jobs, total_labor_minutes}]."""
    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
    end_dt = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc)

    stmt = (
        select(
            TimeClock.technician_id,
            func.count(TimeClock.job_id.distinct()).label("total_jobs"),
            func.coalesce(func.sum(TimeClock.labor_minutes), 0).label("total_labor_minutes"),
        )
        .where(
            TimeClock.clock_in_at >= start_dt,
            TimeClock.clock_in_at <= end_dt,
        )
        .group_by(TimeClock.technician_id)
    )

    rows = db.execute(stmt).all()
    return [
        {
            "technician_id": row.technician_id,
            "total_jobs": int(row.total_jobs),
            "total_labor_minutes": int(row.total_labor_minutes),
        }
        for row in rows
    ]


def revenue_report(start_date: date, end_date: date, db: Session) -> dict:
    """Query Invoices where paid_at in range, return {total_paid, invoice_count, avg_invoice}."""
    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
    end_dt = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc)

    stmt = select(
        func.coalesce(func.sum(Invoice.total), 0).label("total_paid"),
        func.count(Invoice.id).label("invoice_count"),
        func.coalesce(func.avg(Invoice.total), 0).label("avg_invoice"),
    ).where(
        Invoice.paid_at >= start_dt,
        Invoice.paid_at <= end_dt,
        Invoice.deleted_at.is_(None),
    )

    row = db.execute(stmt).one()
    return {
        "total_paid": float(row.total_paid),
        "invoice_count": int(row.invoice_count),
        "avg_invoice": float(row.avg_invoice),
    }
