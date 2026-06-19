"""Public REST API endpoints for external integrations.

All routes require API key authentication (X-API-Key header), not JWT.
Scopes: read:jobs, write:jobs, read:customers, write:customers,
        read:invoices, write:webhooks

Endpoints:
  GET    /v1/jobs              — list jobs          (read:jobs)
  POST   /v1/jobs              — create job         (write:jobs)
  GET    /v1/jobs/{id}         — get job            (read:jobs)
  PATCH  /v1/jobs/{id}         — update job status  (write:jobs)
  GET    /v1/customers         — list customers     (read:customers)
  POST   /v1/customers         — create customer    (write:customers)
  GET    /v1/customers/{id}    — get customer       (read:customers)
  GET    /v1/invoices          — list invoices      (read:invoices)
  POST   /v1/webhooks          — register webhook   (write:webhooks)
  DELETE /v1/webhooks/{id}     — unregister webhook (write:webhooks)

All responses: {"data": ..., "meta": {"request_id": ..., "timestamp": ...}}
"""
from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.webhooks.models import WebhookEndpoint

try:
    from gdx_dispatch.core.api_keys import scope_required
except Exception:

    logging.getLogger(__name__).exception("<module> caught exception")
    def scope_required(scope: str):
        from fastapi import Depends

        async def _noop(request: Request) -> None:
            pass

        return Depends(_noop)


router = APIRouter(prefix="/v1", tags=["Public API v1"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meta(request: Request) -> dict[str, str]:
    return {
        "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _api_response(request: Request, data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder({"data": data, "meta": _meta(request)}),
    )


def _get_tenant_id(request: Request) -> str:
    """Get tenant_id from API key state or fall back to tenant middleware state."""
    tid = getattr(request.state, "api_key_tenant_id", None)
    if tid:
        return tid
    tenant = getattr(request.state, "tenant", None)
    if tenant:
        return str(tenant.get("id", ""))
    raise HTTPException(status_code=401, detail="Authentication required (X-API-Key header missing or invalid)")


def _require_api_key_auth(request: Request) -> None:
    """Ensure request was authenticated via API key."""
    if not getattr(request.state, "api_key_tenant_id", None):
        raise HTTPException(
            status_code=401,
            detail="API key authentication required. Set X-API-Key header.",
        )


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class JobCreate(BaseModel):
    title: str
    customer_id: str | None = None
    scheduled_at: datetime | None = None
    status: str = "Scheduled"


class JobPatch(BaseModel):
    status: str | None = None
    title: str | None = None
    scheduled_at: datetime | None = None


class CustomerCreate(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    address: str | None = None


# ---------------------------------------------------------------------------
# Jobs endpoints
# ---------------------------------------------------------------------------


@router.get("/jobs", summary="List jobs", description="Returns all active jobs for the tenant.")
async def list_jobs(
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(_require_api_key_auth),
    _scope: None = scope_required("read:jobs"),
) -> JSONResponse:
    tenant_id = _get_tenant_id(request)
    try:
        rows = db.execute(
            text(
                "SELECT id, title, status, customer_id, scheduled_at, created_at "
                "FROM jobs WHERE company_id = :tid AND deleted_at IS NULL "
                "ORDER BY created_at DESC LIMIT 200"
            ),
            {"tid": tenant_id},
        ).mappings().all()
        return _api_response(request, [dict(r) for r in rows])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}") from None


@router.post("/jobs", status_code=201, summary="Create job")
async def create_job(
    payload: JobCreate,
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(_require_api_key_auth),
    _scope: None = scope_required("write:jobs"),
) -> JSONResponse:
    tenant_id = _get_tenant_id(request)
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="title is required")
    try:
        row = db.execute(
            text(
                "INSERT INTO jobs (id, title, customer_id, scheduled_at, status, company_id, created_at) "
                "VALUES (:id, :title, :customer_id, :scheduled_at, :status, :company_id, :created_at) "
                "RETURNING id, title, status, customer_id, scheduled_at, created_at"
            ),
            {
                "id": str(uuid.uuid4()),
                "title": title,
                "customer_id": payload.customer_id,
                "scheduled_at": payload.scheduled_at,
                "status": payload.status or "Scheduled",
                "company_id": tenant_id,
                "created_at": datetime.now(UTC),
            },
        ).mappings().first()
        db.commit()
        return _api_response(request, dict(row), 201)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {exc}") from None


@router.get("/jobs/{job_id}", summary="Get job")
async def get_job(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(_require_api_key_auth),
    _scope: None = scope_required("read:jobs"),
) -> JSONResponse:
    tenant_id = _get_tenant_id(request)
    try:
        row = db.execute(
            text(
                "SELECT id, title, status, customer_id, scheduled_at, created_at "
                "FROM jobs WHERE id = :job_id AND company_id = :tid AND deleted_at IS NULL"
            ),
            {"job_id": job_id, "tid": tenant_id},
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return _api_response(request, dict(row))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}") from None


@router.patch("/jobs/{job_id}", summary="Update job status")
async def update_job(
    job_id: str,
    payload: JobPatch,
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(_require_api_key_auth),
    _scope: None = scope_required("write:jobs"),
) -> JSONResponse:
    tenant_id = _get_tenant_id(request)
    # Build dynamic SET clause
    updates: dict[str, Any] = {}
    if payload.status is not None:
        updates["status"] = payload.status
    if payload.title is not None:
        updates["title"] = payload.title.strip()
    if payload.scheduled_at is not None:
        updates["scheduled_at"] = payload.scheduled_at
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["job_id"] = job_id
    updates["tid"] = tenant_id
    try:
        row = db.execute(
            text(
                f"UPDATE jobs SET {set_clause} "
                "WHERE id = :job_id AND company_id = :tid AND deleted_at IS NULL "
                "RETURNING id, title, status, customer_id, scheduled_at, created_at"
            ),
            updates,
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        db.commit()
        return _api_response(request, dict(row))
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {exc}") from None


# ---------------------------------------------------------------------------
# Customers endpoints
# ---------------------------------------------------------------------------


@router.get("/customers", summary="List customers")
async def list_customers(
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(_require_api_key_auth),
    _scope: None = scope_required("read:customers"),
) -> JSONResponse:
    # S122-9 slice 3: ORM-routed so address (EncryptedString) decrypts.
    from gdx_dispatch.models.tenant_models import Customer  # noqa: PLC0415
    try:
        rows = (
            db.query(Customer)
            .filter(Customer.deleted_at.is_(None))
            .order_by(Customer.created_at.desc())
            .limit(200)
            .all()
        )
        items = [
            {
                "id": str(c.id),
                "name": c.name,
                "email": c.email,
                "phone": c.phone,
                "address": c.address,
                "created_at": c.created_at,
            }
            for c in rows
        ]
        return _api_response(request, items)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}") from None


@router.post("/customers", status_code=201, summary="Create customer")
async def create_customer(
    payload: CustomerCreate,
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(_require_api_key_auth),
    _scope: None = scope_required("write:customers"),
) -> JSONResponse:
    tenant_id = _get_tenant_id(request)
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    # ORM write so EncryptedString.process_bind_param fires on address.
    from gdx_dispatch.models.tenant_models import Customer  # noqa: PLC0415
    customer = Customer(
        name=name,
        email=payload.email,
        phone=payload.phone,
        address=payload.address,
        company_id=str(tenant_id),
    )
    try:
        db.add(customer)
        db.commit()
        db.refresh(customer)
        return _api_response(
            request,
            {
                "id": str(customer.id),
                "name": customer.name,
                "email": customer.email,
                "phone": customer.phone,
                "address": customer.address,
                "created_at": customer.created_at,
            },
            201,
        )
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {exc}") from None


@router.get("/customers/{customer_id}", summary="Get customer")
async def get_customer(
    customer_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(_require_api_key_auth),
    _scope: None = scope_required("read:customers"),
) -> JSONResponse:
    # ORM read so address (EncryptedString) decrypts via process_result_value.
    from gdx_dispatch.models.tenant_models import Customer  # noqa: PLC0415
    try:
        cid = uuid.UUID(customer_id)
        customer = (
            db.query(Customer)
            .filter(Customer.id == cid, Customer.deleted_at.is_(None))
            .first()
        )
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        return _api_response(
            request,
            {
                "id": str(customer.id),
                "name": customer.name,
                "email": customer.email,
                "phone": customer.phone,
                "address": customer.address,
                "created_at": customer.created_at,
            },
        )
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=404, detail="Customer not found") from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}") from None


# ---------------------------------------------------------------------------
# Invoices endpoint
# ---------------------------------------------------------------------------


@router.get("/invoices", summary="List invoices")
async def list_invoices(
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(_require_api_key_auth),
    _scope: None = scope_required("read:invoices"),
) -> JSONResponse:
    tenant_id = _get_tenant_id(request)
    try:
        rows = db.execute(
            text(
                "SELECT id, job_id, customer_id, amount, status, created_at "
                "FROM invoices WHERE company_id = :tid AND deleted_at IS NULL "
                "ORDER BY created_at DESC LIMIT 200"
            ),
            {"tid": tenant_id},
        ).mappings().all()
        return _api_response(request, [dict(r) for r in rows])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}") from None


# ---------------------------------------------------------------------------
# Webhook endpoints
# ---------------------------------------------------------------------------


class WebhookCreate(BaseModel):
    url: str
    events: list[str]
    secret: str | None = None


@router.post("/webhooks", status_code=201, summary="Register webhook endpoint")
async def register_webhook(
    payload: WebhookCreate,
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(_require_api_key_auth),
    _scope: None = scope_required("write:webhooks"),
) -> JSONResponse:
    tenant_id = _get_tenant_id(request)
    url = (payload.url or "").strip()
    if not url.startswith("https://"):
        raise HTTPException(status_code=422, detail="url must use HTTPS")
    if not payload.events:
        raise HTTPException(status_code=422, detail="events list must not be empty")
    # ORM write: routes payload.secret through EncryptedString.process_bind_param
    # (S122-9 slice 2 contract). Pinned by raw_sql_on_encrypted_columns_scan.
    # The pre-S122-9 raw INSERT here also inserted a non-existent `company_id`
    # column (tenant plane has no tenant_id by design — connection isolates);
    # silently broken since the schema diverged from the writer. ORM write
    # uses the model as source of truth.
    endpoint = WebhookEndpoint(
        url=url,
        events=list(payload.events or []),
        secret=payload.secret or "",
        is_active=True,
    )
    try:
        db.add(endpoint)
        db.commit()
        db.refresh(endpoint)
        result = {
            "id": str(endpoint.id),
            "url": endpoint.url,
            "events": list(endpoint.events or []),
            "is_active": endpoint.is_active,
            "created_at": endpoint.created_at,
        }
        return _api_response(request, result, 201)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {exc}") from None


@router.delete("/webhooks/{webhook_id}", summary="Unregister webhook endpoint")
async def delete_webhook(
    webhook_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _auth: None = Depends(_require_api_key_auth),
    _scope: None = scope_required("write:webhooks"),
) -> JSONResponse:
    # ORM soft-delete via is_active=False. The pre-S122-9 raw SQL
    # referenced `deleted_at` + `company_id` columns that exist on
    # neither the model nor the actual prod schema — silently 500'd
    # whenever called. Tenant plane isolates by connection, no
    # tenant_id/company_id filter needed.
    try:
        endpoint = db.get(WebhookEndpoint, uuid.UUID(webhook_id))
        if endpoint is None or not endpoint.is_active:
            raise HTTPException(status_code=404, detail="Webhook endpoint not found")
        endpoint.is_active = False
        db.commit()
        return _api_response(request, {"ok": True, "id": webhook_id})
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {exc}") from None
