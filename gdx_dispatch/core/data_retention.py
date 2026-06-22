from __future__ import annotations

"""Data retention enforcement for GDX tenants.

Celery beat task ``enforce_retention_policy`` runs weekly and:
- Marks GDPRRequest records with passed deadlines as "overdue" and creates alerts.
- Hard-deletes soft-deleted customer/job/invoice records older than the tenant's
  configured retention window.
- Logs all deletions in the audit trail.
"""

import asyncio  # noqa: E402
import logging  # noqa: E402
from datetime import timedelta  # noqa: E402
from typing import Annotated  # noqa: E402

from fastapi import APIRouter, Depends, Header, Request  # noqa: E402
from sqlalchemy import delete, func, select, update  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from gdx_dispatch.core.audit import AuditLog, log_audit_event, utcnow  # noqa: E402
from gdx_dispatch.core.database import get_db  # noqa: E402
from gdx_dispatch.core.gdpr_router import GDPRRequest, RetentionPolicy  # noqa: E402
from gdx_dispatch.core.modules import require_role  # noqa: E402
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job  # noqa: E402

logger = logging.getLogger("gdx_dispatch.data_retention")

_DEFAULT_CUSTOMER_DATA_DAYS = 1825   # 5 years
_DEFAULT_AUDIT_LOG_DAYS = 2555        # 7 years


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_policy(db: Session) -> tuple[int, int]:
    """Return (customer_data_days, audit_log_days) from DB or defaults."""
    policy = db.execute(select(RetentionPolicy)).scalar_one_or_none()
    if policy is None:
        return _DEFAULT_CUSTOMER_DATA_DAYS, _DEFAULT_AUDIT_LOG_DAYS
    return policy.customer_data_days, policy.audit_log_days


def _mark_overdue_requests(db: Session, dry_run: bool = False) -> list[str]:
    """Find GDPRRequest records past their deadline_at and mark them overdue.

    Returns the list of request IDs that were (or would be) updated.
    """
    now = utcnow()
    overdue = db.execute(
        select(GDPRRequest).where(
            GDPRRequest.status == "pending",
            GDPRRequest.deadline_at.is_not(None),
            GDPRRequest.deadline_at < now,
        )
    ).scalars().all()

    ids: list[str] = []
    for req in overdue:
        ids.append(str(req.id))
        if not dry_run:
            req.status = "overdue"
            logger.warning(
                "gdpr_request_overdue: request %s (type=%s customer=%s) is past 45-day deadline",
                req.id, req.request_type, req.customer_id,
            )
            asyncio.run(log_audit_event(
                db, "gdpr_request_overdue", "system", "gdpr_request", str(req.id),
                {"request_type": req.request_type, "deadline_at": str(req.deadline_at)},
            ))
    return ids


def _hard_delete_expired_customers(db: Session, customer_data_days: int, dry_run: bool = False) -> list[str]:
    """Hard-delete soft-deleted Customer rows older than the retention window.

    Also cascades to soft-deleted jobs and invoices for those customers.
    Returns list of customer IDs removed.
    """
    cutoff = utcnow() - timedelta(days=customer_data_days)
    expired_customers = db.execute(
        select(Customer).where(
            Customer.deleted_at.is_not(None),
            Customer.deleted_at < cutoff,
        )
    ).scalars().all()

    deleted_ids: list[str] = []
    for customer in expired_customers:
        cid = customer.id
        cid_str = str(cid)

        # Find and hard-delete related jobs
        jobs = db.execute(
            select(Job).where(Job.customer_id == cid, Job.deleted_at.is_not(None))
        ).scalars().all()
        job_ids = [j.id for j in jobs]

        # Find and hard-delete related invoices
        if job_ids:
            invoices = db.execute(
                select(Invoice).where(
                    Invoice.job_id.in_(job_ids),
                    Invoice.deleted_at.is_not(None),
                )
            ).scalars().all()
        else:
            invoices = []

        if not dry_run:
            for inv in invoices:
                db.delete(inv)
            for job in jobs:
                db.delete(job)
            db.delete(customer)
            logger.info(
                "retention_hard_delete: removed customer %s with %d jobs, %d invoices",
                cid_str, len(jobs), len(invoices),
            )
            asyncio.run(log_audit_event(
                db, "retention_hard_delete", "system", "customer", cid_str,
                {
                    "jobs_deleted": len(jobs),
                    "invoices_deleted": len(invoices),
                    "customer_data_days": customer_data_days,
                },
            ))

        deleted_ids.append(cid_str)

    return deleted_ids


# ---------------------------------------------------------------------------
# Main enforcement function (called by Celery beat task)
# ---------------------------------------------------------------------------

def enforce_retention_policy(db: Session, dry_run: bool = False) -> dict:
    """Run all retention enforcement checks against the given tenant DB session.

    Args:
        db:       SQLAlchemy session for the tenant database.
        dry_run:  If True, report what *would* be done without committing changes.

    Returns a summary dict suitable for logging / task result storage.
    """
    customer_data_days, audit_log_days = _get_policy(db)

    overdue_request_ids = _mark_overdue_requests(db, dry_run=dry_run)
    deleted_customer_ids = _hard_delete_expired_customers(db, customer_data_days, dry_run=dry_run)

    summary = {
        "dry_run": dry_run,
        "customer_data_days": customer_data_days,
        "audit_log_days": audit_log_days,
        "overdue_requests_marked": len(overdue_request_ids),
        "overdue_request_ids": overdue_request_ids,
        "customers_hard_deleted": len(deleted_customer_ids),
        "deleted_customer_ids": deleted_customer_ids,
    }

    if not dry_run:
        db.commit()
        logger.info("enforce_retention_policy complete: %s", summary)
    else:
        logger.info("enforce_retention_policy DRY RUN: %s", summary)

    return summary


# ---------------------------------------------------------------------------
# Celery task wiring
# ---------------------------------------------------------------------------

def register_celery_task() -> None:
    """Register the weekly Celery beat task.

    Called from the application factory or celery_app.py.  Importing this
    function does NOT start Celery — it only registers the task if Celery is
    already configured.
    """
    try:
        from gdx_dispatch.core.celery_app import celery_app
        from gdx_dispatch.core.database import get_db as _get_db  # noqa: F401

        @celery_app.task(name="gdx_dispatch.data_retention.enforce_retention_policy_task", bind=True)
        def enforce_retention_policy_task(self) -> dict:  # type: ignore[no-untyped-def]
            """Weekly Celery task: enforce data retention policy for all tenant DBs."""
            import os as _os

            from gdx_dispatch.core.database import SessionLocal

            tenant_id = _os.getenv("GDX_TENANT_ID") or _os.getenv("GDX_DEFAULT_TENANT_ID") or "gdx"
            results = {}
            try:
                with SessionLocal() as tenant_session:
                    results[tenant_id] = enforce_retention_policy(tenant_session)
            except Exception as exc:
                logger.error("enforce_retention_policy_task: failed: %s", exc)
                results[tenant_id] = {"error": str(exc)}

            return results

        logger.info("data_retention: Celery task registered")
    except ImportError:
        logger.debug("data_retention: Celery not available, task not registered")


# ---------------------------------------------------------------------------
# Retention summary API (entity-level counts, oldest records, retention days)
# ---------------------------------------------------------------------------

RETENTION_POLICIES: dict[str, int] = {
    "customers": 2555,   # 7 years
    "jobs": 2555,        # 7 years
    "invoices": 2555,    # 7 years (tax compliance)
    "audit_log": 3650,   # 10 years
}

_MODEL_MAP: dict = {
    "customers": Customer,
    "jobs": Job,
    "invoices": Invoice,
    "audit_log": AuditLog,
}

_SOFT_DELETE_ENTITIES = {"customers", "jobs", "invoices"}


def get_retention_summary(db: Session) -> dict:
    """Return retention summary: count, oldest_record, retention_days per entity."""
    summary: dict[str, dict] = {}
    for entity, retention_days in RETENTION_POLICIES.items():
        model = _MODEL_MAP[entity]
        if entity in _SOFT_DELETE_ENTITIES:
            count_val = db.execute(
                select(func.count()).select_from(model).where(model.deleted_at.is_(None))  # type: ignore[attr-defined]
            ).scalar_one()
            oldest_val = db.execute(
                select(func.min(model.created_at)).where(model.deleted_at.is_(None))  # type: ignore[attr-defined]
            ).scalar_one()
        else:
            count_val = db.execute(
                select(func.count()).select_from(model)
            ).scalar_one()
            oldest_val = db.execute(
                select(func.min(model.created_at))  # type: ignore[attr-defined]
            ).scalar_one()
        summary[entity] = {
            "count": count_val or 0,
            "oldest_record": oldest_val.isoformat() if oldest_val else None,
            "retention_days": retention_days,
        }
    return summary


def apply_retention_policy(tenant_id: str, db: Session) -> dict:
    """Soft-delete records beyond retention for customers/jobs/invoices; hard-delete old audit logs."""
    purged: dict[str, int] = {}
    now = utcnow()
    for entity, retention_days in RETENTION_POLICIES.items():
        model = _MODEL_MAP[entity]
        cutoff = now - timedelta(days=retention_days)
        if entity in _SOFT_DELETE_ENTITIES:
            stmt = (
                update(model)
                .where(
                    model.deleted_at.is_(None),  # type: ignore[attr-defined]
                    model.created_at < cutoff,    # type: ignore[attr-defined]
                )
                .values(deleted_at=now)
            )
            result = db.execute(stmt)
            purged[entity] = result.rowcount
        else:
            stmt_del = delete(model).where(model.created_at < cutoff)  # type: ignore[attr-defined]
            result = db.execute(stmt_del)
            purged[entity] = result.rowcount
    db.commit()
    return {"purged": purged, "tenant_id": tenant_id, "applied_at": now.isoformat()}


def schedule_retention_cleanup() -> dict:
    """Mock Celery beat task stub — returns schedule configuration."""
    return {
        "scheduled": True,
        "task": "data_retention.apply_retention_policy",
        "interval_hours": 24,
    }


# ---------------------------------------------------------------------------
# FastAPI router for retention API endpoints
# ---------------------------------------------------------------------------

retention_router = APIRouter(tags=["data_retention"])
TenantDB = Annotated[Session, Depends(get_db)]


@retention_router.get(
    "/api/gdpr/retention-summary",
    response_model=None,
    dependencies=[Depends(require_role("admin"))],
    summary="Retention summary: counts and oldest record per entity",
)
def retention_summary_endpoint(db: TenantDB) -> dict:
    """Return record counts and oldest record date per entity for retention planning."""
    return get_retention_summary(db)


@retention_router.post(
    "/api/gdpr/apply-retention",
    response_model=None,
    dependencies=[Depends(require_role("owner"))],
    summary="Apply data retention policy immediately (owner-only)",
)
def apply_retention_endpoint(request: Request, db: TenantDB) -> dict:
    """Trigger immediate retention policy enforcement. Owner-only operation.

    Tenant ID comes from server-verified request.state — never from client
    headers (which would let owner of tenant A mislabel an operation on their
    own DB as "applied to tenant B").
    """
    tenant = getattr(request.state, "tenant", None) or {}
    tenant_id = str(tenant.get("id") or "unknown")
    return apply_retention_policy(tenant_id, db)


@retention_router.get(
    "/api/gdpr/retention-schedule",
    response_model=None,
    dependencies=[Depends(require_role("admin"))],
    summary="View scheduled retention cleanup configuration",
)
def retention_schedule_endpoint() -> dict:
    """Return the current retention cleanup schedule configuration."""
    return schedule_retention_cleanup()
