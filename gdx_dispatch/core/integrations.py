"""
gdx_dispatch/core/integrations.py — Zapier-style integration system with configurable webhooks and event triggers.

Provides:
- IntegrationConfig SQLAlchemy model (per-tenant integration configurations)
- TriggerEvent enum (all supported event types)
- Standard payload builders for each event type
- fire_event() — dispatches deliveries to all subscribed integrations
- FastAPI router for CRUD, test, delivery history, and Zapier REST hooks
"""
from __future__ import annotations

import contextlib
import logging
import secrets
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import JSON, Boolean, DateTime, Index, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, log_audit_event, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.pii import EncryptedString
from gdx_dispatch.core.webhooks.models import WebhookDelivery, WebhookEndpoint

try:
    from gdx_dispatch.routers.auth import get_current_user as require_auth
except Exception:
    logging.getLogger(__name__).exception("<module> caught exception")
    def require_auth():
        return None


# ---------------------------------------------------------------------------
# SQLAlchemy Model
# ---------------------------------------------------------------------------

class IntegrationConfig(TenantBase):
    __tablename__ = "integration_configs"
    __table_args__ = (
        Index("ix_integration_configs_tenant_id", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    integration_type: Mapped[str] = mapped_column(String(50), nullable=False)  # zapier|slack|custom_webhook
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    webhook_url: Mapped[str] = mapped_column(String(500), nullable=False)
    # S122-9 slice 2 (2026-05-12): back on EncryptedString. All writers
    # (`gdx_dispatch/core/integrations.py:306`, `:380`, `:637`) are ORM-based, so
    # the TypeDecorator fires consistently. Lint gate
    # `raw_sql_on_encrypted_columns_scan.py` pins the contract.
    secret: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    events: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Event Enum
# ---------------------------------------------------------------------------

class TriggerEvent(str, Enum):
    job_created = "job.created"
    job_completed = "job.completed"
    job_cancelled = "job.cancelled"
    invoice_created = "invoice.created"
    invoice_paid = "invoice.paid"
    customer_created = "customer.created"
    estimate_sent = "estimate.sent"
    estimate_accepted = "estimate.accepted"

    @classmethod
    def all_values(cls) -> list[str]:
        return [e.value for e in cls]


# ---------------------------------------------------------------------------
# Standard Payload Builders
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_job_created_payload(
    tenant_id: str,
    job_id: str,
    customer_name: str,
    job_type: str,
    scheduled_date: str,
    technician: str,
) -> dict[str, Any]:
    return {
        "event": TriggerEvent.job_created.value,
        "tenant_id": tenant_id,
        "job_id": job_id,
        "customer_name": customer_name,
        "job_type": job_type,
        "scheduled_date": scheduled_date,
        "technician": technician,
        "timestamp": _now_iso(),
    }


def build_job_completed_payload(
    tenant_id: str,
    job_id: str,
    customer_name: str,
    completed_at: str,
    invoice_amount: float,
) -> dict[str, Any]:
    return {
        "event": TriggerEvent.job_completed.value,
        "tenant_id": tenant_id,
        "job_id": job_id,
        "customer_name": customer_name,
        "completed_at": completed_at,
        "invoice_amount": invoice_amount,
        "timestamp": _now_iso(),
    }


def build_invoice_paid_payload(
    tenant_id: str,
    invoice_id: str,
    amount_paid: float,
    payment_method: str,
) -> dict[str, Any]:
    return {
        "event": TriggerEvent.invoice_paid.value,
        "tenant_id": tenant_id,
        "invoice_id": invoice_id,
        "amount_paid": amount_paid,
        "payment_method": payment_method,
        "timestamp": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Integration → WebhookEndpoint bridge
# ---------------------------------------------------------------------------

def _get_or_create_endpoint(config: IntegrationConfig, db: Session) -> WebhookEndpoint:
    """Find an existing WebhookEndpoint matching the config's URL, or create one."""
    stmt = select(WebhookEndpoint).where(WebhookEndpoint.url == config.webhook_url)
    endpoint = db.execute(stmt).scalars().first()
    if endpoint is None:
        endpoint = WebhookEndpoint(
            url=config.webhook_url,
            secret=config.secret,
            events=config.events,
            is_active=config.is_active,
        )
        db.add(endpoint)
        db.flush()
    return endpoint


# ---------------------------------------------------------------------------
# fire_event
# ---------------------------------------------------------------------------

def fire_event(
    tenant_id: str,
    event_type: str,
    payload: dict[str, Any],
    db: Session,
) -> list[str]:
    """
    Trigger deliveries for all active IntegrationConfig rows subscribed to event_type.

    Returns list of WebhookDelivery IDs (as strings).
    """
    stmt = select(IntegrationConfig).where(
        IntegrationConfig.tenant_id == tenant_id,
        IntegrationConfig.is_active.is_(True),
    )
    configs = db.execute(stmt).scalars().all()

    delivery_ids: list[str] = []
    now = utcnow()

    for config in configs:
        subscribed_events: list[str] = config.events or []
        if event_type not in subscribed_events:
            continue

        endpoint = _get_or_create_endpoint(config, db)
        idempotency_key = f"{tenant_id}:{event_type}:{uuid4()}"
        delivery = WebhookDelivery(
            endpoint_id=endpoint.id,
            event_type=event_type,
            payload=payload,
            idempotency_key=idempotency_key,
            attempt_count=0,
            status="pending",
            company_id=tenant_id,
        )
        db.add(delivery)
        db.flush()

        # Update trigger timestamp on config
        config.last_triggered_at = now
        delivery_ids.append(str(delivery.id))

    db.commit()
    return delivery_ids


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class IntegrationCreate(BaseModel):
    name: str
    integration_type: str = "custom_webhook"
    webhook_url: str
    events: list[str]
    secret: str = secrets.token_hex(32)


class IntegrationUpdate(BaseModel):
    name: str | None = None
    webhook_url: str | None = None
    events: list[str] | None = None
    is_active: bool | None = None


class IntegrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: str
    integration_type: str
    name: str
    webhook_url: str
    events: list[str]
    is_active: bool
    created_at: datetime
    last_triggered_at: datetime | None
    last_success_at: datetime | None


class ZapierSubscribeRequest(BaseModel):
    target_url: str
    event: str


class ZapierUnsubscribeRequest(BaseModel):
    target_url: str


# ---------------------------------------------------------------------------
# FastAPI Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


def _get_tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", None)
    if not tenant:
        raise HTTPException(status_code=401, detail="Tenant context required")
    return str(tenant["id"])


# Zapier trigger catalogue — must be defined before the /{id} routes to avoid
# the literal string "zapier" being captured as an {id} path parameter.

@router.get("/zapier/triggers")
def zapier_list_triggers():
    """List all available trigger event types for Zapier app configuration."""
    return {
        "triggers": [
            {"event": TriggerEvent.job_created.value, "label": "Job Created"},
            {"event": TriggerEvent.job_completed.value, "label": "Job Completed"},
            {"event": TriggerEvent.job_cancelled.value, "label": "Job Cancelled"},
            {"event": TriggerEvent.invoice_created.value, "label": "Invoice Created"},
            {"event": TriggerEvent.invoice_paid.value, "label": "Invoice Paid"},
            {"event": TriggerEvent.customer_created.value, "label": "Customer Created"},
            {"event": TriggerEvent.estimate_sent.value, "label": "Estimate Sent"},
            {"event": TriggerEvent.estimate_accepted.value, "label": "Estimate Accepted"},
        ]
    }


@router.post("/zapier/subscribe", status_code=201)
async def zapier_subscribe(
    body: ZapierSubscribeRequest,
    request: Request,
    db: Session = Depends(get_db),
    _auth: Any = Depends(require_auth),
):
    """Zapier REST hook subscription — creates an IntegrationConfig of type 'zapier'."""
    tenant_id = _get_tenant_id(request)
    if body.event not in TriggerEvent.all_values():
        raise HTTPException(status_code=400, detail=f"Unknown event: {body.event}")

    config = IntegrationConfig(
        tenant_id=tenant_id,
        integration_type="zapier",
        name=f"Zapier: {body.event}",
        webhook_url=body.target_url,
        secret=secrets.token_hex(32),
        events=[body.event],
        is_active=True,
    )
    db.add(config)
    db.flush()
    await log_audit_event(db, "integration.zapier.subscribe", tenant_id, "integration_config", str(config.id), {"event": body.event, "url": body.target_url})
    db.commit()
    return IntegrationOut.model_validate(config)


@router.delete("/zapier/unsubscribe", status_code=200)
async def zapier_unsubscribe(
    body: ZapierUnsubscribeRequest,
    request: Request,
    db: Session = Depends(get_db),
    _auth: Any = Depends(require_auth),
):
    """Zapier REST hook unsubscribe — deactivates matching zapier IntegrationConfig."""
    tenant_id = _get_tenant_id(request)
    stmt = select(IntegrationConfig).where(
        IntegrationConfig.tenant_id == tenant_id,
        IntegrationConfig.integration_type == "zapier",
        IntegrationConfig.webhook_url == body.target_url,
        IntegrationConfig.is_active.is_(True),
    )
    configs = db.execute(stmt).scalars().all()
    if not configs:
        raise HTTPException(status_code=404, detail="not found")

    for config in configs:
        config.is_active = False
        await log_audit_event(db, "integration.zapier.unsubscribe", tenant_id, "integration_config", str(config.id), {"url": body.target_url})
    db.commit()
    return {"unsubscribed": len(configs)}


# CRUD routes

@router.get("", response_model=None)
def list_integrations_route(
    request: Request,
    db: Session = Depends(get_db),
    _auth: Any = Depends(require_auth),
):
    tenant_id = _get_tenant_id(request)
    try:
        stmt = select(IntegrationConfig).where(IntegrationConfig.tenant_id == tenant_id)
        rows = db.execute(stmt).scalars().all()
        return [IntegrationOut.model_validate(r).model_dump() for r in rows]
    except Exception:
        logging.getLogger(__name__).exception("list_integrations: integration_configs table may not exist")
        with contextlib.suppress(Exception):
            db.rollback()
        raise HTTPException(status_code=500, detail="Failed to retrieve integration configurations") from None


@router.post("", response_model=IntegrationOut, status_code=201)
async def create_integration(
    body: IntegrationCreate,
    request: Request,
    db: Session = Depends(get_db),
    _auth: Any = Depends(require_auth),
):
    tenant_id = _get_tenant_id(request)
    unknown = [e for e in body.events if e not in TriggerEvent.all_values()]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown events: {unknown}")

    config = IntegrationConfig(
        tenant_id=tenant_id,
        integration_type=body.integration_type,
        name=body.name,
        webhook_url=body.webhook_url,
        secret=body.secret,
        events=body.events,
        is_active=True,
    )
    db.add(config)
    db.flush()
    await log_audit_event(db, "integration.created", tenant_id, "integration_config", str(config.id), {"name": body.name, "type": body.integration_type})
    db.commit()
    return config


@router.put("/{integration_id}", response_model=IntegrationOut)
async def update_integration(
    integration_id: str,
    body: IntegrationUpdate,
    request: Request,
    db: Session = Depends(get_db),
    _auth: Any = Depends(require_auth),
):
    tenant_id = _get_tenant_id(request)
    config = db.get(IntegrationConfig, uuid.UUID(integration_id))
    if not config or config.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="not found")

    if body.name is not None:
        config.name = body.name
    if body.webhook_url is not None:
        config.webhook_url = body.webhook_url
    if body.events is not None:
        unknown = [e for e in body.events if e not in TriggerEvent.all_values()]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown events: {unknown}")
        config.events = body.events
    if body.is_active is not None:
        config.is_active = body.is_active

    await log_audit_event(db, "integration.updated", tenant_id, "integration_config", integration_id, body.model_dump(exclude_none=True))
    db.commit()
    return config


@router.delete("/{integration_id}", status_code=204)
async def delete_integration(
    integration_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _auth: Any = Depends(require_auth),
):
    tenant_id = _get_tenant_id(request)
    config = db.get(IntegrationConfig, uuid.UUID(integration_id))
    if not config or config.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="not found")

    await log_audit_event(db, "integration.deleted", tenant_id, "integration_config", integration_id, {"name": config.name})
    db.delete(config)
    db.commit()


@router.post("/{integration_id}/test", status_code=200)
def test_integration(
    integration_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _auth: Any = Depends(require_auth),
):
    """Send a test payload using the job.created event."""
    tenant_id = _get_tenant_id(request)
    config = db.get(IntegrationConfig, uuid.UUID(integration_id))
    if not config or config.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="not found")

    test_payload = build_job_created_payload(
        tenant_id=tenant_id,
        job_id="test-job-001",
        customer_name="Test Customer",
        job_type="test",
        scheduled_date=utcnow().date().isoformat(),
        technician="Test Tech",
    )
    # Temporarily ensure the test event is in scope for fire_event
    original_events = config.events
    if TriggerEvent.job_created.value not in (config.events or []):
        config.events = list(config.events or []) + [TriggerEvent.job_created.value]
        db.flush()

    delivery_ids = fire_event(tenant_id, TriggerEvent.job_created.value, test_payload, db)

    # Restore original events if we patched them
    if config.events != original_events:
        config.events = original_events
        db.commit()

    return {"delivery_ids": delivery_ids, "payload": test_payload}


@router.get("/{integration_id}/deliveries")
def get_deliveries(
    integration_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _auth: Any = Depends(require_auth),
):
    """Return delivery history for this integration's webhook endpoint."""
    tenant_id = _get_tenant_id(request)
    config = db.get(IntegrationConfig, uuid.UUID(integration_id))
    if not config or config.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="not found")

    # Find matching WebhookEndpoint by URL
    ep_stmt = select(WebhookEndpoint).where(WebhookEndpoint.url == config.webhook_url)
    endpoint = db.execute(ep_stmt).scalars().first()
    if not endpoint:
        return {"deliveries": []}

    d_stmt = (
        select(WebhookDelivery)
        .where(WebhookDelivery.endpoint_id == endpoint.id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(100)
    )
    deliveries = db.execute(d_stmt).scalars().all()
    return {
        "deliveries": [
            {
                "id": str(d.id),
                "event_type": d.event_type,
                "status": d.status,
                "attempt_count": d.attempt_count,
                "response_status": d.response_status,
                "last_attempt_at": d.last_attempt_at.isoformat() if d.last_attempt_at else None,
                "created_at": d.created_at.isoformat(),
            }
            for d in deliveries
        ]
    }


# ---------------------------------------------------------------------------
# Integration Marketplace Catalogue
# ---------------------------------------------------------------------------

_INTEGRATION_CATALOGUE: list[dict] = [
    {
        "type": "quickbooks",
        "name": "QuickBooks Online",
        "description": "Sync invoices, customers, and payments with QuickBooks Online.",
        "category": "Accounting",
        "is_oauth": True,
        "logo_icon": "M12 2C6.477 2 2 6.477 2 12s4.477 10 10 10 10-4.477 10-10S17.523 2 12 2zm0 3a7 7 0 110 14A7 7 0 0112 5zm-1 4v6l5-3-5-3z",
        "color": "green",
    },
    {
        "type": "stripe",
        "name": "Stripe",
        "description": "Accept credit card payments and manage billing with Stripe.",
        "category": "Payments",
        "is_oauth": False,
        "logo_icon": "M20 4H4c-1.11 0-2 .89-2 2v12c0 1.11.89 2 2 2h16c1.11 0 2-.89 2-2V6c0-1.11-.89-2-2-2zm0 14H4v-6h16v6zm0-10H4V6h16v2z",
        "color": "purple",
    },
    {
        "type": "google_calendar",
        "name": "Google Calendar",
        "description": "Sync job appointments and technician schedules with Google Calendar.",
        "category": "Scheduling",
        "is_oauth": True,
        "logo_icon": "M19 3h-1V1h-2v2H8V1H6v2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H5V8h14v11zM7 10h5v5H7z",
        "color": "blue",
    },
    {
        "type": "zapier",
        "name": "Zapier",
        "description": "Automate workflows by connecting DispatchApp to 6,000+ apps.",
        "category": "Automation",
        "is_oauth": False,
        "logo_icon": "M13 10V3L4 14h7v7l9-11h-7z",
        "color": "orange",
    },
    {
        "type": "mailchimp",
        "name": "Mailchimp",
        "description": "Sync customers and send automated marketing emails via Mailchimp.",
        "category": "Marketing",
        "is_oauth": False,
        "logo_icon": "M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 14H4l8-8 8 8zm0-12l-8 8-8-8h16z",
        "color": "yellow",
    },
    {
        "type": "twilio",
        "name": "Twilio",
        "description": "Send SMS notifications and make automated calls via Twilio.",
        "category": "Communications",
        "is_oauth": False,
        "logo_icon": "M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 15v-4H7l5-8v4h4l-5 8z",
        "color": "red",
    },
    {
        "type": "google_maps",
        "name": "Google Maps",
        "description": "Verify service addresses and calculate routing distances with Google Maps.",
        "category": "Location",
        "is_oauth": False,
        "logo_icon": "M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5c-1.38 0-2.5-1.12-2.5-2.5s1.12-2.5 2.5-2.5 2.5 1.12 2.5 2.5-1.12 2.5-2.5 2.5z",
        "color": "green",
    },
]

_VALID_INTEGRATION_TYPES = {d["type"] for d in _INTEGRATION_CATALOGUE}

# Public aliases for the marketplace catalogue
SUPPORTED_INTEGRATION_TYPES: list[str] = [d["type"] for d in _INTEGRATION_CATALOGUE]
INTEGRATION_METADATA: dict[str, dict] = {d["type"]: d for d in _INTEGRATION_CATALOGUE}

# Which types require credentials["api_key"] vs OAuth credentials
_OAUTH_TYPES = {"quickbooks", "google_calendar"}
_API_KEY_TYPES = {"stripe", "mailchimp", "twilio", "google_maps", "zapier"}


def list_available_integrations() -> list[dict]:
    """Return the full catalogue of available integrations (no DB needed)."""
    return [dict(d) for d in _INTEGRATION_CATALOGUE]


def connect_integration(
    tenant_id: str,
    integration_type: str,
    credentials: dict,
    db: Session,
) -> dict:
    """
    Store connection credentials for an integration type.

    For OAuth types (quickbooks, google_calendar): expects credentials["access_token"].
    For API key types (stripe, mailchimp, twilio, google_maps, zapier): expects credentials["api_key"].

    Returns {"status": "connected", "integration_type": ..., "id": ...}
    Raises ValueError on invalid type or missing credentials.
    """
    if integration_type not in _VALID_INTEGRATION_TYPES:
        raise ValueError(f"Unknown integration type: {integration_type!r}. Valid: {sorted(_VALID_INTEGRATION_TYPES)}")

    if integration_type in _OAUTH_TYPES:
        if "access_token" not in credentials:
            raise ValueError(f"OAuth integration '{integration_type}' requires credentials['access_token']")
        secret_value = credentials["access_token"]
        if credentials.get("refresh_token"):
            secret_value = f"{credentials['access_token']}:{credentials['refresh_token']}"
    else:
        if "api_key" not in credentials:
            raise ValueError(f"API key integration '{integration_type}' requires credentials['api_key']")
        secret_value = credentials["api_key"]

    config = IntegrationConfig(
        tenant_id=tenant_id,
        integration_type=integration_type,
        name=next(d["name"] for d in _INTEGRATION_CATALOGUE if d["type"] == integration_type),
        webhook_url="",
        secret=secret_value,
        events=[],
        is_active=True,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return {"status": "connected", "integration_type": integration_type, "id": str(config.id)}


def disconnect_integration(
    tenant_id: str,
    integration_type: str,
    db: Session,
) -> dict:
    """
    Deactivate all active IntegrationConfig rows for this tenant + integration_type.

    Returns {"status": "disconnected", "count": n}
    Raises ValueError on invalid type.
    """
    if integration_type not in _VALID_INTEGRATION_TYPES:
        raise ValueError(f"Unknown integration type: {integration_type!r}")

    stmt = select(IntegrationConfig).where(
        IntegrationConfig.tenant_id == tenant_id,
        IntegrationConfig.integration_type == integration_type,
        IntegrationConfig.is_active.is_(True),
    )
    configs = db.execute(stmt).scalars().all()
    for cfg in configs:
        cfg.is_active = False
    db.commit()
    return {"status": "disconnected", "count": len(configs)}


def get_integration_status(
    tenant_id: str,
    integration_type: str,
    db: Session,
) -> dict:
    """
    Return the connection status for a specific integration type.

    Returns {"status": "connected"|"disconnected"|"error", "integration_type": ..., ...}
    """
    if integration_type not in _VALID_INTEGRATION_TYPES:
        return {"status": "unknown", "integration_type": integration_type}

    stmt = select(IntegrationConfig).where(
        IntegrationConfig.tenant_id == tenant_id,
        IntegrationConfig.integration_type == integration_type,
        IntegrationConfig.is_active.is_(True),
    ).order_by(IntegrationConfig.created_at.desc()).limit(1)

    config = db.execute(stmt).scalars().first()
    if config is None:
        return {"status": "disconnected", "integration_type": integration_type}

    last_triggered = config.last_triggered_at.isoformat() if config.last_triggered_at else None
    last_success = config.last_success_at.isoformat() if config.last_success_at else None

    # If triggered but never succeeded, flag as error
    status = "error" if config.last_triggered_at and not config.last_success_at else "connected"

    return {
        "status": status,
        "integration_type": integration_type,
        "id": str(config.id),
        "last_triggered_at": last_triggered,
        "last_success_at": last_success,
    }


def list_integrations(tenant_id: str, db: Session) -> list[dict[str, Any]]:
    """
    Return all supported integration types with their connection status for the tenant.

    Each entry: {type, name, description, connected, last_sync}
    """
    stmt = select(IntegrationConfig).where(
        IntegrationConfig.tenant_id == tenant_id,
        IntegrationConfig.is_active.is_(True),
    )
    active_by_type: dict[str, IntegrationConfig] = {
        row.integration_type: row
        for row in db.execute(stmt).scalars().all()
    }

    result: list[dict[str, Any]] = []
    for entry in _INTEGRATION_CATALOGUE:
        itype = entry["type"]
        config = active_by_type.get(itype)
        result.append({
            "type": itype,
            "name": entry["name"],
            "description": entry["description"],
            "connected": config is not None,
            "last_sync": config.last_success_at.isoformat() if config and config.last_success_at else None,
        })
    return result


def test_connection(tenant_id: str, integration_type: str, db: Session) -> dict[str, Any]:
    """
    Verify that stored credentials are present and mark a successful ping.

    Returns: {ok, type, message}
    """
    if integration_type not in _VALID_INTEGRATION_TYPES:
        return {"ok": False, "type": integration_type, "message": "unknown integration type"}

    stmt = select(IntegrationConfig).where(
        IntegrationConfig.tenant_id == tenant_id,
        IntegrationConfig.integration_type == integration_type,
        IntegrationConfig.is_active.is_(True),
    )
    config = db.execute(stmt).scalars().first()

    if config is None:
        return {"ok": False, "type": integration_type, "message": "not connected"}

    if not config.secret:
        return {"ok": False, "type": integration_type, "message": "credentials missing"}

    # Record a successful ping
    config.last_success_at = utcnow()
    db.commit()

    return {"ok": True, "type": integration_type, "message": "connection OK"}


# ---------------------------------------------------------------------------
# Additional API routes on the existing router
# ---------------------------------------------------------------------------

class ConnectRequest(BaseModel):
    credentials: dict


@router.get("/available")
def list_integrations_available():
    """Return the catalogue of all available integration types."""
    return list_available_integrations()


@router.get("/{integration_type}/status")
def get_integration_status_route(
    integration_type: str,
    request: Request,
    db: Session = Depends(get_db),
    _auth: Any = Depends(require_auth),
):
    tenant_id = _get_tenant_id(request)
    return get_integration_status(tenant_id, integration_type, db)


@router.post("/{integration_type}/connect", status_code=201)
async def connect_integration_route(
    integration_type: str,
    body: ConnectRequest,
    request: Request,
    db: Session = Depends(get_db),
    _auth: Any = Depends(require_auth),
):
    """Initiate a connection for the given integration type."""
    tenant_id = _get_tenant_id(request)
    try:
        result = connect_integration(tenant_id, integration_type, body.credentials, db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await log_audit_event(
        db, "integration.connected", tenant_id,
        "integration_config", result["id"],
        {"integration_type": integration_type},
    )
    return result


@router.delete("/{integration_type}", status_code=200)
async def disconnect_integration_route(
    integration_type: str,
    request: Request,
    db: Session = Depends(get_db),
    _auth: Any = Depends(require_auth),
):
    """Disconnect (deactivate) all connections for the given integration type."""
    tenant_id = _get_tenant_id(request)
    try:
        result = disconnect_integration(tenant_id, integration_type, db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await log_audit_event(
        db, "integration.disconnected", tenant_id,
        "integration_config", integration_type,
        {"integration_type": integration_type, "count": result["count"]},
    )
    return result


# ---------------------------------------------------------------------------
# UI router — serves the integrations marketplace HTML page
# ---------------------------------------------------------------------------

try:
    import os as _os

    from fastapi.templating import Jinja2Templates as _Jinja2Templates

    _TMPL_DIR = _os.path.join(_os.path.dirname(__file__), "..", "templates")
    _templates = _Jinja2Templates(directory=_TMPL_DIR)
    _HAS_TEMPLATES = True
except Exception:
    logging.getLogger(__name__).exception("<module> caught exception")
    _HAS_TEMPLATES = False

ui_router = APIRouter(tags=["integrations-ui"])


def _require_auth_ui(request: Request) -> dict:
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/auth/login"})
    return user


@ui_router.get("/integrations", response_class=HTMLResponse)
def integrations_page(
    request: Request,
    db: Session = Depends(get_db),
):
    _require_auth_ui(request)
    tenant_id_raw = getattr(request.state, "tenant", None)
    tenant_id = str(tenant_id_raw["id"]) if tenant_id_raw else "unknown"

    available = list_available_integrations()

    # Build connected status map for each integration type
    connected_statuses: dict[str, dict] = {}
    for integration in available:
        itype = integration["type"]
        try:
            connected_statuses[itype] = get_integration_status(tenant_id, itype, db)
        except Exception:
            logging.getLogger(__name__).exception("integrations_page caught exception")
            connected_statuses[itype] = {"status": "disconnected", "integration_type": itype}

    # Zapier subscriptions — active IntegrationConfig rows of type "zapier"
    zapier_stmt = select(IntegrationConfig).where(
        IntegrationConfig.tenant_id == tenant_id,
        IntegrationConfig.integration_type == "zapier",
        IntegrationConfig.is_active.is_(True),
    )
    zapier_subscriptions = db.execute(zapier_stmt).scalars().all()

    ctx = {
        "request": request,
        "available_integrations": available,
        "connected_statuses": connected_statuses,
        "zapier_subscriptions": zapier_subscriptions,
        "webhook_endpoints": [],
    }

    if _HAS_TEMPLATES:
        return _templates.TemplateResponse("integrations.html", ctx)
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "templates not found"}, status_code=500)


__all__ = [
    "IntegrationConfig",
    "TriggerEvent",
    "IntegrationCreate",
    "IntegrationUpdate",
    "IntegrationOut",
    "fire_event",
    "build_job_created_payload",
    "build_job_completed_payload",
    "build_invoice_paid_payload",
    "list_available_integrations",
    "list_integrations",
    "connect_integration",
    "disconnect_integration",
    "get_integration_status",
    "test_connection",
    "ConnectRequest",
    "router",
    "ui_router",
]
