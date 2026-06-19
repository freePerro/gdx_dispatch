"""
Service agreements router — warranty/maintenance agreement management.

Reusable templates + per-customer agreements. Tracks start/end dates, price,
included services. Provides an "expiring" query for proactive renewal alerts.

Pattern mirrors gdx_dispatch/routers/proposals.py (CRUD + status transitions + audit).
Gated behind the "jobs" module (service_agreements aliases to jobs in modules.py).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["service_agreements"],
    dependencies=[Depends(require_module("jobs")), Depends(require_permission("settings.write"))],
)


AGREEMENT_STATUSES = ("active", "expired", "cancelled")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


from gdx_dispatch.models.tenant_models import ServiceAgreement, ServiceAgreementTemplate  # noqa: E402

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    default_duration_months: int = Field(default=12, ge=1, le=600)
    default_price: float = Field(default=0, ge=0, le=1_000_000)
    services_included: list[str] = Field(default_factory=list, max_length=100)


class ServiceAgreementIn(BaseModel):
    customer_id: str = Field(min_length=1, max_length=64)
    template_id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    start_date: datetime
    end_date: datetime
    price: float = Field(default=0, ge=0, le=1_000_000)
    services_included: list[str] = Field(default_factory=list, max_length=100)
    notes: str | None = Field(default=None, max_length=5000)


class ServiceAgreementPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    end_date: datetime | None = None
    price: float | None = Field(default=None, ge=0, le=1_000_000)
    services_included: list[str] | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=5000)
    status: str | None = Field(default=None, pattern=r"^(active|expired|cancelled)$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_id(request: Request) -> str:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    tid = str(tenant.get("id") or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


def _user_id(user: Any) -> str:
    if not isinstance(user, dict):
        return "system"
    return str(user.get("sub") or user.get("user_id") or user.get("email") or "system")


def _dumps_services(items: list[str] | None) -> str | None:
    if not items:
        return None
    return json.dumps(list(items))


def _loads_services(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x) for x in data]
    except (ValueError, TypeError):
        log.exception("_loads_services_failed")
        log.warning("service_agreement_services_json_parse_failed raw=%r", raw[:80])
    return []


def _serialize_template(t: ServiceAgreementTemplate) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "company_id": t.company_id,
        "name": t.name,
        "description": t.description,
        "default_duration_months": int(t.default_duration_months or 0),
        "default_price": float(t.default_price or 0),
        "services_included": _loads_services(t.services_included),
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _serialize(a: ServiceAgreement) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "company_id": a.company_id,
        "customer_id": str(a.customer_id) if a.customer_id else None,
        "template_id": str(a.template_id) if a.template_id else None,
        "name": a.name,
        "status": a.status,
        "start_date": a.start_date.isoformat() if a.start_date else None,
        "end_date": a.end_date.isoformat() if a.end_date else None,
        "price": float(a.price or 0),
        "services_included": _loads_services(a.services_included),
        "notes": a.notes,
        "created_by": a.created_by,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


def _get_scoped(db: Session, agreement_id: UUID, tenant_id: str) -> ServiceAgreement:
    row = db.execute(
        select(ServiceAgreement).where(
            ServiceAgreement.id == agreement_id,
            ServiceAgreement.company_id == tenant_id,
            ServiceAgreement.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Service agreement not found")
    return row


def _audit(
    db: Session,
    *,
    tenant_id: str,
    user: Any,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=_user_id(user),
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception(
            "service_agreement_audit_failed action=%s entity_id=%s", action, entity_id
        )
        db.rollback()


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


@router.get("/api/service-agreements/templates", response_model=None)
def list_templates(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = (
        select(ServiceAgreementTemplate)
        .where(
            ServiceAgreementTemplate.deleted_at.is_(None),
        )
        .order_by(ServiceAgreementTemplate.created_at.desc())
    )
    rows = db.execute(stmt).scalars().all()
    return [_serialize_template(r) for r in rows]


@router.post("/api/service-agreements/templates", response_model=None, status_code=201)
def create_template(
    payload: TemplateIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    t = ServiceAgreementTemplate(
        company_id=tenant_id,
        name=payload.name.strip(),
        description=payload.description,
        default_duration_months=int(payload.default_duration_months),
        default_price=Decimal(str(payload.default_price)),
        services_included=_dumps_services(payload.services_included),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="service_agreement_template_created",
        entity_type="service_agreement_template",
        entity_id=str(t.id),
        details={"name": t.name},
        request=request,
    )
    return _serialize_template(t)


# ---------------------------------------------------------------------------
# Agreements
# ---------------------------------------------------------------------------


@router.get("/api/service-agreements", response_model=None)
def list_agreements(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    status: str | None = None,
    customer_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = select(ServiceAgreement).where(
        ServiceAgreement.deleted_at.is_(None),
    )
    if status:
        stmt = stmt.where(ServiceAgreement.status == status)
    if customer_id:
        try:
            stmt = stmt.where(ServiceAgreement.customer_id == UUID(customer_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid customer_id") from None
    stmt = stmt.order_by(ServiceAgreement.created_at.desc()).limit(limit).offset(offset)
    rows = db.execute(stmt).scalars().all()
    return [_serialize(r) for r in rows]


@router.post("/api/service-agreements", response_model=None, status_code=201)
def create_agreement(
    payload: ServiceAgreementIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    if payload.end_date <= payload.start_date:
        raise HTTPException(
            status_code=422, detail="end_date must be after start_date"
        )
    try:
        customer_uuid = UUID(payload.customer_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid customer_id") from None
    template_uuid: UUID | None = None
    if payload.template_id:
        try:
            template_uuid = UUID(payload.template_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid template_id") from None
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        tpl = db.execute(
            select(ServiceAgreementTemplate).where(
                ServiceAgreementTemplate.id == template_uuid,
                ServiceAgreementTemplate.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not tpl:
            raise HTTPException(status_code=404, detail="Template not found")

    a = ServiceAgreement(
        company_id=tenant_id,
        customer_id=customer_uuid,
        template_id=template_uuid,
        name=payload.name.strip(),
        status="active",
        start_date=payload.start_date,
        end_date=payload.end_date,
        price=Decimal(str(payload.price)),
        services_included=_dumps_services(payload.services_included),
        notes=payload.notes,
        created_by=_user_id(user),
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="service_agreement_created",
        entity_type="service_agreement",
        entity_id=str(a.id),
        details={"name": a.name, "customer_id": str(a.customer_id)},
        request=request,
    )
    return _serialize(a)


@router.get("/api/service-agreements/expiring", response_model=None)
def list_expiring(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    days: int = 30,
) -> list[dict[str, Any]]:
    days = max(1, min(int(days or 30), 3650))
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days)
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = (
        select(ServiceAgreement)
        .where(
            ServiceAgreement.deleted_at.is_(None),
            ServiceAgreement.status == "active",
            ServiceAgreement.end_date >= now,
            ServiceAgreement.end_date <= cutoff,
        )
        .order_by(ServiceAgreement.end_date.asc())
    )
    rows = db.execute(stmt).scalars().all()
    return [_serialize(r) for r in rows]


@router.get("/api/service-agreements/{agreement_id}", response_model=None)
def get_agreement(
    agreement_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    return _serialize(_get_scoped(db, agreement_id, tenant_id))


@router.patch("/api/service-agreements/{agreement_id}", response_model=None)
def update_agreement(
    agreement_id: UUID,
    payload: ServiceAgreementPatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    a = _get_scoped(db, agreement_id, tenant_id)
    data = payload.model_dump(exclude_unset=True)

    if "end_date" in data and data["end_date"] is not None:
        if data["end_date"] <= a.start_date:
            raise HTTPException(
                status_code=422, detail="end_date must be after start_date"
            )
        a.end_date = data["end_date"]
    if "name" in data and data["name"] is not None:
        a.name = data["name"]
    if "notes" in data:
        a.notes = data["notes"]
    if "price" in data and data["price"] is not None:
        a.price = Decimal(str(data["price"]))
    if "services_included" in data and data["services_included"] is not None:
        a.services_included = _dumps_services(data["services_included"])
    if "status" in data and data["status"] is not None:
        a.status = data["status"]

    db.commit()
    db.refresh(a)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="service_agreement_updated",
        entity_type="service_agreement",
        entity_id=str(a.id),
        details={"fields": list(data.keys())},
        request=request,
    )
    return _serialize(a)


@router.post("/api/service-agreements/{agreement_id}/cancel", response_model=None)
def cancel_agreement(
    agreement_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    a = _get_scoped(db, agreement_id, tenant_id)
    if a.status == "cancelled":
        raise HTTPException(status_code=400, detail="Agreement already cancelled")
    a.status = "cancelled"
    db.commit()
    db.refresh(a)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="service_agreement_cancelled",
        entity_type="service_agreement",
        entity_id=str(a.id),
        request=request,
    )
    return _serialize(a)
