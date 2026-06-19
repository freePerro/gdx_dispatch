"""
GDPR / CCPA data-rights router (Sprint: data-subject rights).

Endpoints:
  GET  /api/gdpr/export-my-data                 — current user self-export
  GET  /api/gdpr/export-customer/{customer_id}  — admin full customer dump
  POST /api/gdpr/delete-customer/{customer_id}  — admin soft-delete + PII redact
  POST /api/ccpa/opt-out/{customer_id}          — admin CCPA opt-out

All reads are tenant-scoped via request.state.tenant["id"]. All mutations are
append-logged via log_audit_event_sync. Admin routes (2-4) use require_role.

ORM models used: Customer, Job, Invoice from gdx_dispatch.models.tenant_models.
Tables without ORM models (audit_logs, estimates, communications) use text().
"""
from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job, User
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["gdpr"],
    dependencies=[Depends(require_module("customers"))],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tenant_id(request: Request) -> str:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    tid = str(tenant.get("id") or "")
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


def _user_id(user: dict | None) -> str:
    if not isinstance(user, dict):
        return "system"
    return str(user.get("sub") or user.get("user_id") or "system")


def _to_uuid(value: str) -> _uuid.UUID:
    """Convert a string to UUID for ORM column comparison."""
    return _uuid.UUID(value)


def _serialize_customer(c: Customer) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "name": c.name,
        "email": c.email,
        "phone": c.phone,
        "address": c.address,
        "customer_type": c.customer_type,
        "company_id": c.company_id,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "deleted_at": c.deleted_at.isoformat() if c.deleted_at else None,
    }


def _fetch_customer(db: Session, customer_id: str, tenant_id: str) -> Customer | None:
    return db.execute(
        select(Customer).where(
            Customer.id == _to_uuid(customer_id),
            Customer.company_id == tenant_id,
        )
    ).scalar_one_or_none()


def _serialize_job(j: Job) -> dict[str, Any]:
    return {
        "id": str(j.id),
        "title": j.title,
        "lifecycle_stage": j.lifecycle_stage,
        "dispatch_status": j.dispatch_status,
        "billing_status": j.billing_status,
        "scheduled_at": j.scheduled_at.isoformat() if j.scheduled_at else None,
        "completed_at": j.completed_at.isoformat() if j.completed_at else None,
        "created_at": j.created_at.isoformat() if j.created_at else None,
    }


def _serialize_invoice(inv: Invoice) -> dict[str, Any]:
    return {
        "id": str(inv.id),
        "invoice_number": inv.invoice_number,
        "total": float(inv.total) if inv.total is not None else None,
        "status": inv.status,
        "created_at": inv.created_at.isoformat() if inv.created_at else None,
    }


# ---------------------------------------------------------------------------
# 1. Self-export — any authenticated user
# ---------------------------------------------------------------------------

@router.get("/api/gdpr/export-my-data", response_model=None)
def export_my_data(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return the current user's profile, jobs they touched, and their audit events."""
    tenant_id = _tenant_id(request)
    uid = _user_id(user)

    profile: dict[str, Any] = {"user_id": uid, "tenant_id": tenant_id, "role": user.get("role")}
    try:
        import uuid as _uuid_mod
        try:
            uid_uuid = _uuid_mod.UUID(str(uid))
        except (ValueError, AttributeError):
            uid_uuid = None
        user_row = db.execute(
            select(User).where(User.id == uid_uuid).limit(1)
        ).scalar_one_or_none() if uid_uuid is not None else None
        if user_row:
            profile.update({"id": user_row.id, "email": user_row.email, "role": user_row.role, "created_at": str(user_row.created_at) if user_row.created_at else None})
    except SQLAlchemyError:
        log.exception("gdpr_export_my_data_user_lookup_failed")

    # Jobs — ORM
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    jobs: list[dict] = []
    try:
        job_rows = db.execute(
            select(Job).where(
                (Job.assigned_to == uid) | (Job.assigned_to == str(user.get("email") or "")),
            )
        ).scalars().all()
        jobs = [_serialize_job(j) for j in job_rows]
    except SQLAlchemyError:
        log.exception("gdpr_export_my_data_jobs_lookup_failed")

    # Audit logs — no ORM model; use text()
    audit_events: list[dict] = []
    try:
        audit_rows = db.execute(
            text(
                """
                SELECT id, action, entity_type, entity_id, details, created_at
                FROM audit_logs
                WHERE tenant_id = :tenant_id AND user_id = :uid
                ORDER BY created_at ASC
                """
            ),
            {"tenant_id": tenant_id, "uid": uid},
        ).mappings().all()
        audit_events = [dict(r) for r in audit_rows]
    except SQLAlchemyError:
        log.exception("gdpr_export_my_data_audit_lookup_failed")

    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=uid,
            action="gdpr_self_export",
            entity_type="user",
            entity_id=uid,
            details={"jobs": len(jobs), "audit_events": len(audit_events)},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("gdpr_self_export_audit_failed")

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": profile,
        "data": {
            "profile": profile,
            "jobs": jobs,
            "audit_events": audit_events,
        },
    }


# ---------------------------------------------------------------------------
# 2. Full customer dump — admin/owner only
# ---------------------------------------------------------------------------

@router.get(
    "/api/gdpr/export-customer/{customer_id}",
    response_model=None,
    dependencies=[Depends(require_role("admin", "owner"))],
)
def export_customer(
    customer_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    cid = str(customer_id)

    customer = _fetch_customer(db, cid, tenant_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    customer_dict = _serialize_customer(customer)

    # Jobs — ORM
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    jobs: list[dict] = []
    try:
        job_rows = db.execute(
            select(Job).where(
                Job.customer_id == _to_uuid(cid),
            )
        ).scalars().all()
        jobs = [_serialize_job(j) for j in job_rows]
    except SQLAlchemyError:
        log.exception("gdpr_export_customer_jobs_failed")

    # Invoices — ORM
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    invoices: list[dict] = []
    try:
        inv_rows = db.execute(
            select(Invoice).where(
                Invoice.customer_id == _to_uuid(cid),
            )
        ).scalars().all()
        invoices = [_serialize_invoice(inv) for inv in inv_rows]
    except SQLAlchemyError:
        log.exception("gdpr_export_customer_invoices_failed")

    def _safe_query(sql: str, params: dict, label: str) -> list[dict]:
        """Fallback for tables without ORM models."""
        try:
            rows = db.execute(text(sql), params).mappings().all()
            return [dict(r) for r in rows]
        except SQLAlchemyError:
            log.exception("gdpr_export_customer_%s_failed", label)
            return []

    # Tables without ORM models — keep as text()
    estimates = _safe_query(
        """
        SELECT id, status, total, created_at
        FROM estimates
        WHERE customer_id = :cid
        """,
        {"cid": cid},
        "estimates",
    )

    payments = _safe_query(
        """
        SELECT p.id, p.amount, p.method, p.payment_date, p.created_at
        FROM payments p
        JOIN invoices i ON i.id = p.invoice_id
        WHERE i.customer_id = :cid
        """,
        {"cid": cid},
        "payments",
    )

    communications = _safe_query(
        """
        SELECT id, channel, direction, body, created_at
        FROM communications
        WHERE customer_id = :cid
        """,
        {"cid": cid},
        "communications",
    )

    audit_events = _safe_query(
        """
        SELECT id, action, entity_type, entity_id, user_id, details, created_at
        FROM audit_logs
        WHERE tenant_id = :tenant_id AND entity_id = :cid
        ORDER BY created_at ASC
        """,
        {"tenant_id": tenant_id, "cid": cid},
        "audit_events",
    )

    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=_user_id(user),
            action="gdpr_customer_exported",
            entity_type="customer",
            entity_id=cid,
            details={
                "jobs": len(jobs),
                "invoices": len(invoices),
                "estimates": len(estimates),
                "payments": len(payments),
                "communications": len(communications),
            },
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("gdpr_customer_export_audit_failed")

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "customer": customer_dict,
        "data": {
            "customer": customer_dict,
            "jobs": jobs,
            "invoices": invoices,
            "estimates": estimates,
            "payments": payments,
            "communications": communications,
            "audit_events": audit_events,
        },
    }


# ---------------------------------------------------------------------------
# 3. Soft-delete + PII redaction — admin/owner only
# ---------------------------------------------------------------------------

@router.post(
    "/api/gdpr/delete-customer/{customer_id}",
    response_model=None,
    dependencies=[Depends(require_role("admin", "owner"))],
)
def delete_customer_gdpr(
    customer_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    cid = str(customer_id)

    customer = _fetch_customer(db, cid, tenant_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    now = datetime.now(timezone.utc)
    try:
        customer.name = "Redacted"
        customer.email = None
        customer.phone = None
        customer.address = None
        customer.deleted_at = now
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("gdpr_delete_customer_update_failed")
        raise HTTPException(status_code=500, detail="Failed to redact customer") from exc

    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=_user_id(user),
            action="gdpr_customer_deleted",
            entity_type="customer",
            entity_id=cid,
            details={"customer_id": cid, "redacted_fields": ["name", "email", "phone", "address"]},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("gdpr_customer_delete_audit_failed")

    return {"ok": True, "customer_id": cid, "deleted_at": now.isoformat()}


# ---------------------------------------------------------------------------
# 4. CCPA opt-out — admin/owner only
# ---------------------------------------------------------------------------

@router.post(
    "/api/ccpa/opt-out/{customer_id}",
    response_model=None,
    dependencies=[Depends(require_role("admin", "owner"))],
)
def ccpa_opt_out(
    customer_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    cid = str(customer_id)

    customer = _fetch_customer(db, cid, tenant_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    # sms_opt_out / email_opt_out — added by migration 111_ccpa_and_integrations
    # Gracefully handle missing columns (pre-migration) by checking first
    try:
        col_check = db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'customers' AND column_name = 'sms_opt_out'"
            )
        ).fetchone()
        if col_check:
            db.execute(
                text(
                    """
                    UPDATE customers
                    SET sms_opt_out = :true_val,
                        email_opt_out = :true_val
                    WHERE id = :cid AND company_id = :tenant_id
                    """
                ),
                {"cid": cid, "tenant_id": tenant_id, "true_val": True},
            )
        else:
            log.warning("ccpa_opt_out: sms_opt_out column missing — run migration 111")
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("ccpa_opt_out_update_failed")
        raise HTTPException(
            status_code=500,
            detail="Failed to set opt-out flags",
        ) from exc

    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=_user_id(user),
            action="ccpa_opt_out",
            entity_type="customer",
            entity_id=cid,
            details={"customer_id": cid, "sms_opt_out": True, "email_opt_out": True},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("ccpa_opt_out_audit_failed")

    return {"ok": True, "customer_id": cid, "sms_opt_out": True, "email_opt_out": True}
