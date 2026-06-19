from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_role

log = logging.getLogger(__name__)

# Global search is available to any authenticated tenant user. It doesn't map
# to a single feature module, so we gate on role membership only (any signed-in
# role) rather than on a feature toggle.
router = APIRouter(
    prefix="/api/search",
    tags=["search"],
    dependencies=[Depends(require_role("admin", "owner", "user", "tech", "dispatcher", "superadmin"))],
)


@router.get("", response_model=None)
def global_search(
    q: str = Query(min_length=1, max_length=120),
    db: Session = Depends(get_db),
) -> dict[str, list[dict[str, Any]]]:
    try:
        term = q.strip()
        if not term:
            return {"jobs": [], "customers": [], "invoices": [], "estimates": []}

        pattern = f"%{term.lower()}%"

        jobs: list[dict[str, Any]] = []
        customers: list[dict[str, Any]] = []
        invoices: list[dict[str, Any]] = []
        estimates: list[dict[str, Any]] = []

        try:
            from sqlalchemy import func as _func

            from gdx_dispatch.models.tenant_models import Job
            job_rows = db.execute(
                select(Job).where(
                    Job.deleted_at.is_(None),
                    _func.lower(_func.coalesce(Job.title, "")).like(pattern),
                ).order_by(Job.created_at.desc()).limit(5)
            ).scalars().all()
            jobs = [{"id": str(j.id), "number": str(j.id), "title": j.title} for j in job_rows]
        except SQLAlchemyError:
            log.exception("search_jobs_failed")

        try:
            from sqlalchemy import func as _func

            from gdx_dispatch.models.tenant_models import Customer
            customer_rows = db.execute(
                select(Customer).where(
                    Customer.deleted_at.is_(None),
                    (
                        _func.lower(_func.coalesce(Customer.name, "")).like(pattern)
                        | _func.lower(_func.coalesce(Customer.phone, "")).like(pattern)
                        | _func.lower(_func.coalesce(Customer.email, "")).like(pattern)
                    ),
                ).order_by(Customer.created_at.desc()).limit(5)
            ).scalars().all()
            customers = [
                {
                    "id": str(c.id),
                    "name": c.name,
                    "phone": c.phone,
                    "email": c.email,
                }
                for c in customer_rows
            ]
        except SQLAlchemyError:
            log.exception("search_customers_failed")

        try:
            from sqlalchemy import func as _func

            from gdx_dispatch.models.tenant_models import Invoice
            invoice_rows = db.execute(
                select(Invoice).where(
                    Invoice.deleted_at.is_(None),
                    _func.lower(_func.coalesce(Invoice.invoice_number, "")).like(pattern),
                ).order_by(Invoice.created_at.desc()).limit(5)
            ).scalars().all()
            invoices = [{"id": str(i.id), "number": i.invoice_number, "total": float(i.total or 0)} for i in invoice_rows]
        except SQLAlchemyError:
            log.exception("search_invoices_failed")

        try:
            from sqlalchemy import func as _func

            from gdx_dispatch.modules.proposals.models import Estimate
            estimate_rows = db.execute(
                select(Estimate).where(
                    Estimate.deleted_at.is_(None),
                    _func.lower(_func.coalesce(Estimate.estimate_number, "")).like(pattern),
                ).order_by(Estimate.created_at.desc()).limit(5)
            ).scalars().all()
            estimates = [
                {
                    "id": str(e.id),
                    "number": e.estimate_number,
                    "label": e.label,
                }
                for e in estimate_rows
            ]
        except SQLAlchemyError:
            log.exception("search_estimates_failed")

        return {
            "jobs": jobs,
            "customers": customers,
            "invoices": invoices,
            "estimates": estimates,
        }
    except Exception:
        log.exception("global_search_failed")
        raise HTTPException(status_code=500, detail="Failed to run search") from None
