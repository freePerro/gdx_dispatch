from __future__ import annotations

import asyncio
import csv
import io
import json
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import AuditLog, log_audit_event, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_role
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job

data_export_router = APIRouter(tags=["gdpr_export"])

# In-memory job tracker for scheduled exports (replace with Redis/DB in production)
EXPORT_JOBS: dict[str, dict] = {}

TenantDB = Annotated[Session, Depends(get_db)]

# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class ExportScheduleRequest(BaseModel):
    email: str
    format: Literal["json", "csv", "zip"] = "zip"


class DoNotSellRequest(BaseModel):
    customer_id: str


class RightsRequest(BaseModel):
    customer_id: str
    request_type: Literal["deletion", "access", "portability"]
    notes: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_dict(row: object) -> dict:
    """Convert an ORM row to a serializable dict."""
    result = {}
    for col in row.__table__.columns:  # type: ignore[attr-defined]
        val = getattr(row, col.name)
        if isinstance(val, (datetime,)):
            result[col.name] = val.isoformat()
        elif isinstance(val, uuid.UUID):
            result[col.name] = str(val)
        elif isinstance(val, dict):
            result[col.name] = val
        else:
            result[col.name] = val
    return result


def _rows_to_csv(rows: list) -> str:
    """Convert a list of ORM rows to a CSV string."""
    if not rows:
        return ""
    buf = io.StringIO()
    fieldnames = [col.name for col in rows[0].__table__.columns]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(_row_to_dict(row))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# DATA MAP — static definition of what tenant data is stored
# ---------------------------------------------------------------------------

_DATA_MAP = [
    {
        "entity": "customers",
        "fields": ["id", "name", "email", "phone", "address", "source", "notes", "created_at", "deleted_at"],
        "retention_days": 2555,
        "pii": True,
        "location": "tenant_db",
        "description": "Customer contact records, encrypted at rest",
    },
    {
        "entity": "jobs",
        "fields": ["id", "customer_id", "title", "description", "lifecycle_stage", "dispatch_status",
                   "billing_status", "scheduled_at", "completed_at", "assigned_to", "source", "created_at"],
        "retention_days": 2555,
        "pii": False,
        "location": "tenant_db",
        "description": "Service job records linked to customers",
    },
    {
        "entity": "invoices",
        "fields": ["id", "job_id", "invoice_number", "billing_type", "subtotal", "tax_amount", "total",
                   "status", "sent_at", "paid_at", "created_at"],
        "retention_days": 2555,
        "pii": False,
        "location": "tenant_db",
        "description": "Invoice records for billing; 7-year retention for tax compliance",
    },
    {
        "entity": "audit_log",
        "fields": ["id", "event_type", "actor_id", "actor_role", "entity_type", "entity_id",
                   "payload", "ip_address", "created_at", "hash"],
        "retention_days": 3650,
        "pii": False,
        "location": "tenant_db",
        "description": "Immutable tamper-evident audit trail; 10-year retention",
    },
]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@data_export_router.get(
    "/api/gdpr/export",
    response_class=StreamingResponse,
    dependencies=[Depends(require_role("admin"))],
    summary="Full tenant data export as ZIP",
)
def gdpr_full_export(
    db: TenantDB,
    tenant_id: str = Header(default="unknown", alias="X-Tenant-ID"),
) -> StreamingResponse:
    """Export all tenant data as a ZIP file containing CSV files per entity and a metadata JSON."""
    customers = list(db.execute(select(Customer)).scalars().all())
    jobs = list(db.execute(select(Job)).scalars().all())
    invoices = list(db.execute(select(Invoice)).scalars().all())
    audit_logs = list(db.execute(select(AuditLog)).scalars().all())

    export_date = datetime.now(timezone.utc).isoformat()
    metadata = {
        "export_date": export_date,
        "tenant_id": tenant_id,
        "row_counts": {
            "customers": len(customers),
            "jobs": len(jobs),
            "invoices": len(invoices),
            "audit_log": len(audit_logs),
        },
    }

    io.BytesIO()
    with io.BytesIO() as zbuf:
        import zipfile
        with zipfile.ZipFile(zbuf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("customers.csv", _rows_to_csv(customers))
            zf.writestr("jobs.csv", _rows_to_csv(jobs))
            zf.writestr("invoices.csv", _rows_to_csv(invoices))
            zf.writestr("audit_log.csv", _rows_to_csv(audit_logs))
            zf.writestr("metadata.json", json.dumps(metadata, indent=2))
        zip_bytes = zbuf.getvalue()

    asyncio.run(log_audit_event(
        db, "gdpr_full_export", "system", "tenant", tenant_id,
        {"row_counts": metadata["row_counts"], "export_date": export_date},
    ))
    db.commit()

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"tenant_export_{date_str}.zip"

    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@data_export_router.post(
    "/api/gdpr/export/schedule",
    response_model=None,
    dependencies=[Depends(require_role("admin"))],
    summary="Schedule an async export job",
)
def schedule_export(
    body: ExportScheduleRequest,
    db: TenantDB,
    tenant_id: str = Header(default="unknown", alias="X-Tenant-ID"),
) -> dict:
    """Queue an async export job; returns job_id for status polling."""
    job_id = str(uuid.uuid4())
    EXPORT_JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "email": body.email,
        "format": body.format,
        "tenant_id": tenant_id,
        "created_at": utcnow().isoformat(),
    }
    asyncio.run(log_audit_event(
        db, "gdpr_export_scheduled", "system", "tenant", tenant_id,
        {"job_id": job_id, "email": body.email, "format": body.format},
    ))
    db.commit()
    return {"job_id": job_id, "status": "queued", "email": body.email}


@data_export_router.get(
    "/api/gdpr/export/status/{job_id}",
    response_model=None,
    dependencies=[Depends(require_role("admin"))],
    summary="Check async export job status",
)
def export_status(job_id: str) -> dict:
    """Return the status of a scheduled export job."""
    job = EXPORT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")
    return job


@data_export_router.get(
    "/api/gdpr/data-map",
    response_model=None,
    dependencies=[Depends(require_role("admin"))],
    summary="Data map: entities stored, fields, retention, PII status",
)
def data_map() -> list[dict]:
    """Return the static data map describing all stored tenant data."""
    return _DATA_MAP


@data_export_router.post(
    "/api/ccpa/do-not-sell",
    response_model=None,
    dependencies=[Depends(require_role("admin"))],
    summary="CCPA opt-out: set do_not_sell flag on a customer",
)
def ccpa_do_not_sell(
    body: DoNotSellRequest,
    db: TenantDB,
    tenant_id: str = Header(default="unknown", alias="X-Tenant-ID"),
) -> dict:
    """Mark a customer as opted out of data selling (CCPA do-not-sell)."""
    from uuid import UUID
    customer = db.execute(
        select(Customer).where(Customer.id == UUID(body.customer_id))
    ).scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    customer.metadata_ = {**(customer.metadata_ or {}), "do_not_sell": True}
    asyncio.run(log_audit_event(
        db, "ccpa_do_not_sell", "system", "customer", body.customer_id,
        {"do_not_sell": True},
    ))
    db.commit()
    return {"ok": True}


@data_export_router.get(
    "/api/ccpa/rights-requests",
    response_model=None,
    dependencies=[Depends(require_role("admin"))],
    summary="List pending CCPA rights requests",
)
def ccpa_rights_requests_list(db: TenantDB) -> list[dict]:
    """Return all CCPA rights requests and do-not-sell events from the audit log."""
    rows = db.execute(
        select(AuditLog)
        .where(AuditLog.event_type.in_(["ccpa_rights_request", "ccpa_do_not_sell"]))
        .order_by(AuditLog.created_at.desc())
    ).scalars().all()
    return [
        {
            "id": str(r.id),
            "customer_id": r.entity_id,
            "event_type": r.event_type,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "payload": r.payload or {},
        }
        for r in rows
    ]


@data_export_router.post(
    "/api/ccpa/rights-requests",
    response_model=None,
    dependencies=[Depends(require_role("admin"))],
    summary="Submit a CCPA rights request (deletion/access/portability)",
)
def ccpa_submit_rights_request(
    body: RightsRequest,
    db: TenantDB,
    tenant_id: str = Header(default="unknown", alias="X-Tenant-ID"),
) -> dict:
    """Submit a CCPA rights request; logged to audit trail and queued for review."""
    audit = asyncio.run(log_audit_event(
        db, "ccpa_rights_request", "system", "customer", body.customer_id,
        {"type": body.request_type, "status": "pending", "notes": body.notes},
    ))
    db.commit()
    return {"ok": True, "request_id": str(audit.id)}
