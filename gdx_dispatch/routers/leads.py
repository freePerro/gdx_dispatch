"""
Leads router — sales pipeline and landing (marketing) lead intake.

- LandingLead: raw form submissions from marketing site (name/email/phone/utm/...).
- Lead: qualified prospect with pipeline stage worked by sales reps.

Gated behind the "customers" module. Every query is tenant-scoped by
``company_id == request.state.tenant["id"]``. Every mutation logs an audit
event via ``log_audit_event_sync``.

Pattern follows gdx_dispatch/routers/appointments.py and gdx_dispatch/routers/proposals.py
(inline model + CRUD + state transitions + audit).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["leads"],
    dependencies=[Depends(require_module("customers"))],
)


LANDING_STATUSES = ("new", "contacted", "discarded")
LEAD_STAGES = ("new", "contacted", "qualified", "quoted", "won", "lost")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


from gdx_dispatch.models.tenant_models import LandingLead, Lead  # noqa: E402

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class LandingLeadIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=30)
    source: str | None = Field(default=None, max_length=100)
    message: str | None = Field(default=None, max_length=5000)
    referrer: str | None = Field(default=None, max_length=500)
    utm_campaign: str | None = Field(default=None, max_length=200)
    utm_source: str | None = Field(default=None, max_length=200)
    utm_medium: str | None = Field(default=None, max_length=200)


class LandingLeadStatusIn(BaseModel):
    status: str = Field(pattern=r"^(new|contacted|discarded)$")


class LeadIn(BaseModel):
    landing_lead_id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=30)
    address: str | None = Field(default=None, max_length=500)
    stage: str = Field(
        default="new", pattern=r"^(new|contacted|qualified|quoted|won|lost)$"
    )
    estimated_value: float = Field(default=0, ge=0, le=10_000_000)
    source: str | None = Field(default=None, max_length=100)
    assigned_to: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=10000)


class LeadPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=30)
    address: str | None = Field(default=None, max_length=500)
    estimated_value: float | None = Field(default=None, ge=0, le=10_000_000)
    source: str | None = Field(default=None, max_length=100)
    assigned_to: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=10000)


class StageIn(BaseModel):
    stage: str = Field(pattern=r"^(new|contacted|qualified|quoted|won|lost)$")


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
    return str(
        user.get("sub") or user.get("user_id") or user.get("email") or "system"
    )


def _parse_uuid(raw: str | None, field: str) -> UUID | None:
    if raw is None or raw == "":
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail=f"Invalid {field}") from None


def _serialize_landing(r: LandingLead) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "company_id": r.company_id,
        "name": r.name,
        "email": r.email,
        "phone": r.phone,
        "source": r.source,
        "message": r.message,
        "referrer": r.referrer,
        "utm_campaign": r.utm_campaign,
        "utm_source": r.utm_source,
        "utm_medium": r.utm_medium,
        "status": r.status,
        "contacted_at": r.contacted_at.isoformat() if r.contacted_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _serialize_lead(l: Lead) -> dict[str, Any]:
    return {
        "id": str(l.id),
        "company_id": l.company_id,
        "landing_lead_id": str(l.landing_lead_id) if l.landing_lead_id else None,
        "name": l.name,
        "email": l.email,
        "phone": l.phone,
        "address": l.address,
        "stage": l.stage,
        "estimated_value": float(l.estimated_value or 0),
        "source": l.source,
        "assigned_to": l.assigned_to,
        "notes": l.notes,
        "converted_customer_id": str(l.converted_customer_id)
        if l.converted_customer_id
        else None,
        "converted_at": l.converted_at.isoformat() if l.converted_at else None,
        "last_contact_at": l.last_contact_at.isoformat() if l.last_contact_at else None,
        "created_by": l.created_by,
        "created_at": l.created_at.isoformat() if l.created_at else None,
        "updated_at": l.updated_at.isoformat() if l.updated_at else None,
    }


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
            "leads_audit_failed action=%s entity_id=%s", action, entity_id
        )
        db.rollback()


def _get_landing_scoped(db: Session, ll_id: UUID, tenant_id: str) -> LandingLead:
    row = db.execute(
        select(LandingLead).where(
            LandingLead.id == ll_id,
            LandingLead.company_id == tenant_id,
            LandingLead.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Landing lead not found")
    return row


def _get_lead_scoped(db: Session, lead_id: UUID, tenant_id: str) -> Lead:
    row = db.execute(
        select(Lead).where(
            Lead.id == lead_id,
            Lead.company_id == tenant_id,
            Lead.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Lead not found")
    return row


# ---------------------------------------------------------------------------
# Landing leads endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/api/landing-leads",
    response_model=None,
    dependencies=[Depends(require_permission("leads.read"))],
)
def list_landing_leads(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    status: str | None = None,
    source: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = select(LandingLead).where(
        LandingLead.deleted_at.is_(None),
    )
    if status:
        stmt = stmt.where(LandingLead.status == status)
    if source:
        stmt = stmt.where(LandingLead.source == source)
    stmt = stmt.order_by(LandingLead.created_at.desc()).limit(limit).offset(offset)
    rows = db.execute(stmt).scalars().all()
    return [_serialize_landing(r) for r in rows]


@router.post(
    "/api/landing-leads",
    response_model=None,
    status_code=201,
    dependencies=[Depends(require_permission("leads.write"))],
)
def create_landing_lead(
    payload: LandingLeadIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    r = LandingLead(
        company_id=tenant_id,
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        source=payload.source,
        message=payload.message,
        referrer=payload.referrer,
        utm_campaign=payload.utm_campaign,
        utm_source=payload.utm_source,
        utm_medium=payload.utm_medium,
        status="new",
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="landing_lead_created",
        entity_type="landing_lead",
        entity_id=str(r.id),
        details={"source": r.source, "utm_campaign": r.utm_campaign},
        request=request,
    )
    return _serialize_landing(r)


@router.patch(
    "/api/landing-leads/{ll_id}/status",
    response_model=None,
    dependencies=[Depends(require_permission("leads.write"))],
)
def update_landing_lead_status(
    ll_id: UUID,
    payload: LandingLeadStatusIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    r = _get_landing_scoped(db, ll_id, tenant_id)
    r.status = payload.status
    if payload.status == "contacted" and r.contacted_at is None:
        r.contacted_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(r)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="landing_lead_status_updated",
        entity_type="landing_lead",
        entity_id=str(r.id),
        details={"status": r.status},
        request=request,
    )
    return _serialize_landing(r)


from typing import Literal as _Literal


@router.delete(
    "/api/landing-leads/{ll_id}",
    response_model=None,
    status_code=200,
    # BFLA gate. Migrated from require_role(...) to the codebase-canonical
    # permission-key model (D-leads-authz-sweep): leads.delete resolves to
    # admin/owner/sales/dispatcher via BUILTIN_ROLES; require_permission's
    # upstream escape hatch keeps admin/owner lockout-proof.
    dependencies=[Depends(require_permission("leads.delete"))],
)
def delete_landing_lead(
    ll_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    # Audit §2: constrain reason to a closed set so unbounded user input
    # can't end up in audit JSONB (length-DoS or future audit-viewer XSS).
    reason: _Literal["spam", "manual"] = "manual",
) -> dict[str, Any]:
    """Soft-delete a landing lead.

    Sets `deleted_at = NOW()` and `status = 'discarded'` so the row disappears
    from the list view (which filters `deleted_at IS NULL`). The `reason`
    query param (`spam` or `manual`) is preserved in the audit log for
    later analysis of why marketing-site submissions were rejected.

    Tenant isolation is the connection itself (Depends(get_db) opens
    a session bound to the tenant's own DB). Per the three-plane invariant
    in CLAUDE.md, we do NOT add `WHERE company_id = :tenant_id` filters on
    tenant-plane models — that's the 2026-04-22 NULL-document trap. The
    sibling handlers in this file still use _get_landing_scoped (the
    grandfathered pattern); their fix is a separate refactor.
    """
    tenant_id = _tenant_id(request)
    row = db.execute(
        select(LandingLead).where(
            LandingLead.id == ll_id,
            LandingLead.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Landing lead not found")

    row.deleted_at = datetime.now(timezone.utc)
    row.status = "discarded"

    # Audit §3: write the audit row in the SAME transaction as the
    # delete. log_audit_event_sync only flushes — it doesn't commit. If
    # the audit insert raises (schema drift, RLS rejection), get_db's
    # finally:db.close() rolls back the entire transaction including the
    # delete, so we never end up soft-deleted-without-audit-trail (SOC2
    # evidence gap).
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=_user_id(user),
        action="landing_lead_deleted",
        entity_type="landing_lead",
        entity_id=str(row.id),
        details={"reason": reason},
        request=request,
    )
    db.commit()
    return {"id": str(row.id), "deleted_at": row.deleted_at.isoformat()}


@router.post(
    "/api/landing-leads/{ll_id}/convert-to-lead",
    response_model=None,
    status_code=201,
    dependencies=[Depends(require_permission("leads.write"))],
)
def convert_landing_lead_to_lead(
    ll_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    ll = _get_landing_scoped(db, ll_id, tenant_id)
    lead = Lead(
        company_id=tenant_id,
        landing_lead_id=ll.id,
        name=(ll.name or "Unnamed Lead").strip() or "Unnamed Lead",
        email=ll.email,
        phone=ll.phone,
        stage="new",
        estimated_value=Decimal("0"),
        source=ll.source,
        notes=ll.message,
        created_by=_user_id(user),
    )
    db.add(lead)
    # Mark landing lead as contacted once promoted.
    if ll.status == "new":
        ll.status = "contacted"
        if ll.contacted_at is None:
            ll.contacted_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(lead)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="landing_lead_converted",
        entity_type="landing_lead",
        entity_id=str(ll.id),
        details={"lead_id": str(lead.id)},
        request=request,
    )
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="lead_created",
        entity_type="lead",
        entity_id=str(lead.id),
        details={"landing_lead_id": str(ll.id)},
        request=request,
    )
    return _serialize_lead(lead)


# ---------------------------------------------------------------------------
# Leads endpoints (sales pipeline)
# ---------------------------------------------------------------------------


@router.get(
    "/api/leads",
    response_model=None,
    dependencies=[Depends(require_permission("leads.read"))],
)
def list_leads(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    stage: str | None = None,
    assigned_to: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = select(Lead).where(
        Lead.deleted_at.is_(None),
    )
    if stage:
        stmt = stmt.where(Lead.stage == stage)
    if assigned_to:
        stmt = stmt.where(Lead.assigned_to == assigned_to)
    stmt = stmt.order_by(Lead.created_at.desc()).limit(limit).offset(offset)
    rows = db.execute(stmt).scalars().all()
    return [_serialize_lead(r) for r in rows]


@router.get(
    "/api/leads/pipeline-summary",
    response_model=None,
    dependencies=[Depends(require_permission("leads.read"))],
)
def pipeline_summary(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = select(Lead).where(
        Lead.deleted_at.is_(None),
    )
    rows = db.execute(stmt).scalars().all()
    summary: dict[str, int] = {s: 0 for s in LEAD_STAGES}
    for l in rows:
        if l.stage in summary:
            summary[l.stage] += 1
    return summary


@router.post(
    "/api/leads",
    response_model=None,
    status_code=201,
    dependencies=[Depends(require_permission("leads.write"))],
)
def create_lead(
    payload: LeadIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    landing_uuid = _parse_uuid(payload.landing_lead_id, "landing_lead_id")
    lead = Lead(
        company_id=tenant_id,
        landing_lead_id=landing_uuid,
        name=payload.name.strip(),
        email=payload.email,
        phone=payload.phone,
        address=payload.address,
        stage=payload.stage,
        estimated_value=Decimal(str(payload.estimated_value)),
        source=payload.source,
        assigned_to=payload.assigned_to,
        notes=payload.notes,
        created_by=_user_id(user),
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="lead_created",
        entity_type="lead",
        entity_id=str(lead.id),
        details={"stage": lead.stage, "assigned_to": lead.assigned_to},
        request=request,
    )
    return _serialize_lead(lead)


@router.get(
    "/api/leads/{lead_id}",
    response_model=None,
    dependencies=[Depends(require_permission("leads.read"))],
)
def get_lead(
    lead_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    return _serialize_lead(_get_lead_scoped(db, lead_id, tenant_id))


@router.patch(
    "/api/leads/{lead_id}",
    response_model=None,
    dependencies=[Depends(require_permission("leads.write"))],
)
def update_lead(
    lead_id: UUID,
    payload: LeadPatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    lead = _get_lead_scoped(db, lead_id, tenant_id)
    for field in ("name", "email", "phone", "address", "source", "assigned_to", "notes"):
        val = getattr(payload, field, None)
        if val is not None:
            setattr(lead, field, val)
    if payload.estimated_value is not None:
        lead.estimated_value = Decimal(str(payload.estimated_value))
    db.commit()
    db.refresh(lead)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="lead_updated",
        entity_type="lead",
        entity_id=str(lead.id),
        details={},
        request=request,
    )
    return _serialize_lead(lead)


@router.post(
    "/api/leads/{lead_id}/advance-stage",
    response_model=None,
    dependencies=[Depends(require_permission("leads.write"))],
)
def advance_stage(
    lead_id: UUID,
    payload: StageIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    lead = _get_lead_scoped(db, lead_id, tenant_id)
    if payload.stage not in LEAD_STAGES:
        raise HTTPException(status_code=422, detail="Invalid stage")
    old = lead.stage
    lead.stage = payload.stage
    if payload.stage == "contacted" and lead.last_contact_at is None:
        lead.last_contact_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(lead)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="lead_stage_advanced",
        entity_type="lead",
        entity_id=str(lead.id),
        details={"from": old, "to": lead.stage},
        request=request,
    )
    return _serialize_lead(lead)


@router.post(
    "/api/leads/{lead_id}/convert-to-customer",
    response_model=None,
    dependencies=[Depends(require_permission("leads.write"))],
)
def convert_to_customer(
    lead_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    lead = _get_lead_scoped(db, lead_id, tenant_id)

    new_customer_id = uuid4()
    now = datetime.now(timezone.utc)

    try:
        from gdx_dispatch.models.tenant_models import Customer
        customer = Customer(
            id=new_customer_id,
            company_id=tenant_id,
            name=lead.name or "",
            email=lead.email,
            phone=lead.phone,
            address=lead.address,
            created_at=now,
        )
        db.add(customer)
        db.flush()
    except (OperationalError, ProgrammingError) as exc:
        log.exception("convert_to_customer_insert_failed lead_id=%s", lead_id)
        db.rollback()
        return {
            "lead_id": str(lead.id),
            "customer_id": None,
            "converted": False,
            "reason": f"customers table unavailable: {type(exc).__name__}",
        }
    except Exception:
        log.exception("convert_to_customer_unexpected lead_id=%s", lead_id)
        db.rollback()
        return {
            "lead_id": str(lead.id),
            "customer_id": None,
            "converted": False,
            "reason": "unexpected error",
        }

    lead.converted_customer_id = new_customer_id
    lead.converted_at = now
    lead.stage = "won"
    db.commit()
    db.refresh(lead)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="lead_converted_to_customer",
        entity_type="lead",
        entity_id=str(lead.id),
        details={"customer_id": str(new_customer_id)},
        request=request,
    )
    return {
        "lead_id": str(lead.id),
        "customer_id": str(new_customer_id),
        "converted": True,
    }


@router.delete(
    "/api/leads/{lead_id}",
    response_model=None,
    status_code=204,
    # BFLA gate, migrated to the permission-key model alongside the rest
    # of the router (D-leads-authz-sweep). leads.delete →
    # admin/owner/sales/dispatcher via BUILTIN_ROLES.
    dependencies=[Depends(require_permission("leads.delete"))],
)
def delete_lead(
    lead_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    lead = _get_lead_scoped(db, lead_id, tenant_id)
    lead.deleted_at = datetime.now(timezone.utc)
    db.commit()
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="lead_deleted",
        entity_type="lead",
        entity_id=str(lead.id),
        details={},
        request=request,
    )
    return None
