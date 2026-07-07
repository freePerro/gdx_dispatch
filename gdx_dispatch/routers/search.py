from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
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


def _like(column, pattern: str):
    return func.lower(func.coalesce(column, "")).like(pattern)


# 2026-07-07 — wired to the Ctrl+K palette. Matching notes:
#   - Jobs match job_number OR title OR customer name (pre-fix: title only,
#     and the payload returned the job's UUID as "number").
#   - Invoices match invoice_number OR customer name.
#   - Estimates match estimate_number OR label OR jobsite_address OR
#     customer name.
#   - Customer.address is EncryptedString (S122-9) — not LIKE-searchable in
#     SQL, so address lookup goes through estimates.jobsite_address only.
#     Full customer-address search is blocked on the
#     D-S122-9-customer-search-encryption decision (sidecar tsvector).
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
            from gdx_dispatch.models.tenant_models import Customer, Job

            job_rows = db.execute(
                select(Job, Customer.name)
                .outerjoin(Customer, Job.customer_id == Customer.id)
                .where(
                    Job.deleted_at.is_(None),
                    (
                        _like(Job.title, pattern)
                        | _like(Job.job_number, pattern)
                        | _like(Customer.name, pattern)
                    ),
                )
                .order_by(Job.created_at.desc())
                .limit(5)
            ).all()
            jobs = [
                {
                    "id": str(j.id),
                    "number": j.job_number,
                    "title": j.title,
                    "customer_name": customer_name,
                }
                for j, customer_name in job_rows
            ]
        except SQLAlchemyError:
            log.exception("search_jobs_failed")

        try:
            from gdx_dispatch.models.tenant_models import Customer

            customer_rows = db.execute(
                select(Customer).where(
                    Customer.deleted_at.is_(None),
                    (
                        _like(Customer.name, pattern)
                        | _like(Customer.phone, pattern)
                        | _like(Customer.email, pattern)
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
            from gdx_dispatch.models.tenant_models import Customer, Invoice

            invoice_rows = db.execute(
                select(Invoice, Customer.name)
                .outerjoin(Customer, Invoice.customer_id == Customer.id)
                .where(
                    Invoice.deleted_at.is_(None),
                    (
                        _like(Invoice.invoice_number, pattern)
                        | _like(Customer.name, pattern)
                    ),
                )
                .order_by(Invoice.created_at.desc())
                .limit(5)
            ).all()
            invoices = [
                {
                    "id": str(i.id),
                    "number": i.invoice_number,
                    "total": float(i.total or 0),
                    "status": i.status,
                    "customer_name": customer_name,
                }
                for i, customer_name in invoice_rows
            ]
        except SQLAlchemyError:
            log.exception("search_invoices_failed")

        try:
            from gdx_dispatch.models.tenant_models import Customer
            from gdx_dispatch.modules.proposals.models import Estimate

            estimate_rows = db.execute(
                select(Estimate, Customer.name)
                .outerjoin(Customer, Estimate.customer_id == Customer.id)
                .where(
                    Estimate.deleted_at.is_(None),
                    (
                        _like(Estimate.estimate_number, pattern)
                        | _like(Estimate.label, pattern)
                        | _like(Estimate.jobsite_address, pattern)
                        | _like(Customer.name, pattern)
                    ),
                )
                .order_by(Estimate.created_at.desc())
                .limit(5)
            ).all()
            estimates = [
                {
                    "id": str(e.id),
                    "number": e.estimate_number,
                    "label": e.label,
                    "customer_name": customer_name,
                }
                for e, customer_name in estimate_rows
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
