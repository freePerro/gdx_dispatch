"""
Leads router — sales pipeline and landing (marketing) lead intake.

- LandingLead: raw form submissions from marketing site (name/email/phone/utm/...).
- Lead: qualified prospect with pipeline stage worked by sales reps.

Gated behind the "customers" module. Every query is tenant-scoped by
``company_id == request.state.tenant["id"]``. Every mutation logs an audit
event via ``log_audit_event_sync``.

Pattern follows gdx_dispatch/routers/appointments.py
(inline model + CRUD + state transitions + audit).
"""
from __future__ import annotations

import logging
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import has_permission, require_module, require_permission
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.custom_fields import (
    CustomFieldDefinition,
    CustomFieldValueUpsert,
    _list_values_for_entity,
    _upsert_values_for_entity,
)
from gdx_dispatch.routers.estimates import (
    _serialize_estimate,
    create_draft_estimate_record,
)

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["leads"],
    dependencies=[Depends(require_module("customers"))],
)


# "promoted" (2026-08-08): the honest exit for a submission that entered the
# pipeline. Conversion used to stamp status="contacted" + contacted_at even
# though nothing was sent and nobody called — a fabricated contact fact
# (same sin as inventing labor hours). contacted_at is only ever written by
# an actual outreach event: the manual Contacted button or an inbox send.
# "completed" (2026-08-13): handled entirely outside the pipeline — question
# answered, booked directly, duplicate of a walk-in. Terminal like promoted/
# discarded, but records "done" rather than "entered pipeline" or "junk".
LANDING_STATUSES = ("new", "contacted", "completed", "promoted", "discarded")
LEAD_STAGES = ("new", "contacted", "qualified", "quoted", "won", "lost")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


from gdx_dispatch.models.tenant_models import AppSettings, Customer, LandingLead, Lead  # noqa: E402

DEFAULT_LEAD_SOURCES = [
    "Google",
    "Referral",
    "Repeat Customer",
    "Drive-by / Yard Sign",
    "Social Media",
    "Direct Mail",
    "Other",
]

_DEFAULT_LEAD_FIELDS = [
    {
        "field_key": "job_kind",
        "label": "Job kind",
        "field_type": "select",
        "options": json.dumps(["Repair", "New door", "New opener", "Door + opener"]),
        "required": False,
        "sort_order": 1,
    },
    {
        "field_key": "door_count",
        "label": "Door count",
        "field_type": "number",
        "options": None,
        "required": False,
        "sort_order": 2,
    },
    {
        "field_key": "door_size",
        "label": "Door size",
        "field_type": "text",
        "options": None,
        "required": False,
        "sort_order": 3,
    },
    {
        "field_key": "door_options",
        "label": "Door options",
        "field_type": "text",
        "options": None,
        "required": False,
        "sort_order": 4,
    },
    {
        "field_key": "opener",
        "label": "Opener",
        "field_type": "text",
        "options": None,
        "required": False,
        "sort_order": 5,
    },
]


def next_business_day(settings: AppSettings | None, from_date: date | None = None) -> date:
    tz_name = getattr(settings, "timezone", None) or "America/Chicago"
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("UTC")

    today = from_date or datetime.now(tz).date()

    mask = getattr(settings, "default_workdays", None)
    mask = 31 if mask is None or int(mask) <= 0 else int(mask)

    try:
        from gdx_dispatch.core.time_off import holiday_calendar
        holidays = {h["date"] for h in holiday_calendar(settings) if isinstance(h, dict) and "date" in h}
    except Exception:
        holidays = set()

    candidate = today + timedelta(days=1)
    for _ in range(30):
        bit = 1 << candidate.weekday()
        if (mask & bit) and candidate.isoformat() not in holidays:
            return candidate
        candidate += timedelta(days=1)
    return today + timedelta(days=1)


def _ensure_lead_custom_field_definitions(db: Session, tenant_id: str) -> None:
    try:
        existing = db.execute(
            select(CustomFieldDefinition).where(
                CustomFieldDefinition.company_id == tenant_id,
                CustomFieldDefinition.entity_type == "lead",
                CustomFieldDefinition.deleted_at.is_(None),
            )
        ).scalars().first()
        if existing is None:
            now = datetime.now(timezone.utc)
            for f in _DEFAULT_LEAD_FIELDS:
                db.add(
                    CustomFieldDefinition(
                        id=uuid4(),
                        company_id=tenant_id,
                        entity_type="lead",
                        field_key=f["field_key"],
                        label=f["label"],
                        field_type=f["field_type"],
                        options=f["options"],
                        required=f["required"],
                        sort_order=f["sort_order"],
                        created_at=now,
                        updated_at=now,
                    )
                )
            db.flush()
    except Exception:
        db.rollback()


def _find_possible_duplicate_lead(
    db: Session, tenant_id: str, lead: Lead
) -> dict[str, Any] | None:
    base = select(Lead).where(
        Lead.deleted_at.is_(None),
        Lead.company_id == tenant_id,
        Lead.stage.not_in(["won", "lost"]),
        Lead.id != lead.id,
    )
    if lead.origin_ref:
        m = db.execute(base.where(Lead.origin_ref == lead.origin_ref)).scalars().first()
        if m:
            return {"id": str(m.id), "name": m.name, "stage": m.stage, "reason": "origin_ref"}
    if lead.email:
        email_clean = lead.email.strip().lower()
        if email_clean:
            m = db.execute(base.where(func.lower(Lead.email) == email_clean)).scalars().first()
            if m:
                return {"id": str(m.id), "name": m.name, "stage": m.stage, "reason": "email"}
    digits = "".join(ch for ch in (lead.phone or "") if ch.isdigit())
    if len(digits) >= 10:
        stripped = Lead.phone
        for sep in (" ", "-", "(", ")", ".", "+"):
            stripped = func.replace(stripped, sep, "")
        m = db.execute(base.where(stripped.like(f"%{digits[-10:]}"))).scalars().first()
        if m:
            return {"id": str(m.id), "name": m.name, "stage": m.stage, "reason": "phone"}
    return None



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
    # "promoted" is deliberately absent: only the convert endpoint may set it.
    status: str = Field(pattern=r"^(new|contacted|completed|discarded)$")


# LeadsView renders capitalized stages ("Contacted") and echoes them back on
# create/edit/advance; the lowercase-only patterns were silently 422-ing
# those paths. Normalize case before validation instead of trusting every
# client to know the canonical casing.
def _lower_stage(v: Any) -> Any:
    return v.strip().lower() if isinstance(v, str) else v


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
    follow_up_date: date | None = None
    origin_ref: str | None = Field(default=None, max_length=120)

    _norm_stage = field_validator("stage", mode="before")(_lower_stage)


class LeadPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=30)
    address: str | None = Field(default=None, max_length=500)
    estimated_value: float | None = Field(default=None, ge=0, le=10_000_000)
    source: str | None = Field(default=None, max_length=100)
    assigned_to: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=10000)
    # Tier-6 (2026-07): the edit dialog has always sent stage; the PATCH
    # silently dropped it, so only the separate advance-stage button worked.
    stage: str | None = Field(default=None, pattern=r"^(new|contacted|qualified|quoted|won|lost)$")
    follow_up_date: date | None = None

    _norm_stage = field_validator("stage", mode="before")(_lower_stage)


class LeadIntakeIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=30)
    address: str | None = Field(default=None, max_length=500)
    source: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=10000)
    origin_ref: str | None = Field(default=None, max_length=120)
    follow_up_date: date | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class StageIn(BaseModel):
    stage: str = Field(pattern=r"^(new|contacted|qualified|quoted|won|lost)$")

    _norm_stage = field_validator("stage", mode="before")(_lower_stage)


class ConvertToCustomerIn(BaseModel):
    """Optional body for convert-to-customer.

    stage says what the conversion is FOR: "won" (they booked work — the
    service-call path) or "quoted" (we're drafting an estimate). Anything
    else stays on the explicit advance-stage endpoint.
    """

    stage: str = Field(default="won", pattern=r"^(won|quoted)$")

    _norm_stage = field_validator("stage", mode="before")(_lower_stage)


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
        "follow_up_date": l.follow_up_date.isoformat() if getattr(l, "follow_up_date", None) else None,
        "estimate_id": str(l.estimate_id) if getattr(l, "estimate_id", None) else None,
        "origin_ref": getattr(l, "origin_ref", None),
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
    # Promotion is a pipeline fact, not a contact fact. contacted_at is
    # deliberately NOT stamped here — it stays NULL until a real outreach
    # event (the Contacted button, or an inbox send) records one. A prior
    # contacted_at, if any, survives as evidence of that earlier contact.
    ll.status = "promoted"
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
    follow_up: str | None = None,
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

    if follow_up:
        tenant_id = _tenant_id(request)
        settings = db.execute(
            select(AppSettings).limit(1)
        ).scalars().first()
        tz_name = getattr(settings, "timezone", None) or "America/Chicago"
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("UTC")
        today = datetime.now(tz).date()

        stmt = stmt.where(Lead.stage.not_in(["won", "lost"]))
        if follow_up == "overdue":
            stmt = stmt.where(Lead.follow_up_date.isnot(None), Lead.follow_up_date < today)
            stmt = stmt.order_by(Lead.follow_up_date.asc(), Lead.created_at.desc())
        elif follow_up == "today":
            stmt = stmt.where(Lead.follow_up_date == today)
            stmt = stmt.order_by(Lead.created_at.desc())
        elif follow_up == "upcoming":
            stmt = stmt.where(Lead.follow_up_date.isnot(None), Lead.follow_up_date > today)
            stmt = stmt.order_by(Lead.follow_up_date.asc(), Lead.created_at.desc())
        else:
            stmt = stmt.order_by(Lead.created_at.desc())
    else:
        stmt = stmt.order_by(Lead.created_at.desc())

    stmt = stmt.limit(limit).offset(offset)
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


@router.get(
    "/api/leads/follow-up-summary",
    response_model=None,
    dependencies=[Depends(require_permission("leads.read"))],
)
def follow_up_summary(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    tenant_id = _tenant_id(request)
    settings = db.execute(
        select(AppSettings).limit(1)
    ).scalars().first()
    tz_name = getattr(settings, "timezone", None) or "America/Chicago"
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("UTC")
    today = datetime.now(tz).date()

    open_leads = db.execute(
        select(Lead).where(
            Lead.deleted_at.is_(None),
            Lead.stage.not_in(["won", "lost"]),
            Lead.follow_up_date.isnot(None),
        )
    ).scalars().all()

    overdue = sum(1 for l in open_leads if l.follow_up_date and l.follow_up_date < today)
    due_today = sum(1 for l in open_leads if l.follow_up_date == today)
    return {"overdue": overdue, "due_today": due_today}


@router.get(
    "/api/leads/intake-form",
    response_model=None,
)
def get_lead_intake_form(
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not (
        has_permission(request, db, "leads.intake")
        or has_permission(request, db, "leads.write")
    ):
        raise HTTPException(status_code=403, detail="Permission denied")
    tenant_id = _tenant_id(request)
    _ensure_lead_custom_field_definitions(db, tenant_id)
    return {
        "custom_fields": _list_values_for_entity(db, tenant_id, "lead", ""),
        "sources": DEFAULT_LEAD_SOURCES,
    }


@router.post(
    "/api/leads/intake",
    response_model=None,
    status_code=201,
)
def create_lead_intake(
    payload: LeadIntakeIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not (
        has_permission(request, db, "leads.intake")
        or has_permission(request, db, "leads.write")
    ):
        raise HTTPException(status_code=403, detail="Permission denied")
    tenant_id = _tenant_id(request)

    settings = db.execute(
        select(AppSettings).limit(1)
    ).scalars().first()
    follow_up = payload.follow_up_date or next_business_day(settings)

    lead = Lead(
        company_id=tenant_id,
        name=payload.name.strip(),
        email=payload.email.strip() if payload.email else None,
        phone=payload.phone.strip() if payload.phone else None,
        address=payload.address.strip() if payload.address else None,
        source=payload.source.strip() if payload.source else None,
        notes=payload.notes.strip() if payload.notes else None,
        origin_ref=payload.origin_ref.strip() if payload.origin_ref else None,
        follow_up_date=follow_up,
        stage="new",
        assigned_to=None,
        created_by=_user_id(user),
    )
    db.add(lead)
    db.flush()

    if payload.custom_fields:
        _ensure_lead_custom_field_definitions(db, tenant_id)
        _upsert_values_for_entity(
            db,
            request,
            user,
            "lead",
            str(lead.id),
            CustomFieldValueUpsert(values=payload.custom_fields),
            commit=False,
        )

    possible_dup = _find_possible_duplicate_lead(db, tenant_id, lead)

    matched_cust = None
    matched = _find_matching_customer(db, email=lead.email, phone=lead.phone)
    if matched:
        matched_cust = {
            "id": str(matched.id),
            "name": matched.name,
            "phone": matched.phone,
            "email": matched.email,
        }

    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=_user_id(user),
        action="lead_intake_created",
        entity_type="lead",
        entity_id=str(lead.id),
        details={
            "source": lead.source,
            "follow_up_date": lead.follow_up_date.isoformat() if lead.follow_up_date else None,
            "has_duplicate_warning": possible_dup is not None,
            "matched_customer": matched_cust is not None,
        },
        request=request,
    )

    db.commit()
    db.refresh(lead)

    return {
        "lead": _serialize_lead(lead),
        "matched_customer": matched_cust,
        "possible_duplicate": possible_dup,
    }


@router.get(
    "/api/leads/by-estimate/{estimate_id}",
    response_model=None,
    dependencies=[Depends(require_permission("leads.read"))],
)
def get_lead_by_estimate(
    estimate_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    lead = db.execute(
        select(Lead).where(
            Lead.company_id == tenant_id,
            Lead.estimate_id == estimate_id,
            Lead.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found for estimate")
    data = _serialize_lead(lead)
    data["custom_fields"] = _list_values_for_entity(db, tenant_id, "lead", str(lead.id))
    return data

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
        follow_up_date=payload.follow_up_date,
        origin_ref=payload.origin_ref,
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


@router.get(
    "/api/leads/{lead_id}/custom-fields",
    response_model=None,
)
def get_lead_custom_fields(
    lead_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    if not (
        has_permission(request, db, "leads.read")
        or has_permission(request, db, "leads.write")
        or has_permission(request, db, "leads.intake")
    ):
        raise HTTPException(status_code=403, detail="Permission denied")
    tenant_id = _tenant_id(request)
    _get_lead_scoped(db, lead_id, tenant_id)
    return _list_values_for_entity(db, tenant_id, "lead", str(lead_id))


@router.put(
    "/api/leads/{lead_id}/custom-fields",
    response_model=None,
    dependencies=[Depends(require_permission("leads.write"))],
)
def put_lead_custom_fields(
    lead_id: UUID,
    payload: CustomFieldValueUpsert,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    _get_lead_scoped(db, lead_id, tenant_id)
    _ensure_lead_custom_field_definitions(db, tenant_id)
    return _upsert_values_for_entity(db, request, user, "lead", str(lead_id), payload)


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
    # Mirror advance_stage's side effect so the edit-dialog path doesn't
    # diverge: first contact stamps last_contact_at (audit catch).
    if payload.stage == "contacted" and lead.last_contact_at is None:
        lead.last_contact_at = datetime.now(timezone.utc)
    for field in ("name", "email", "phone", "address", "source", "assigned_to", "notes", "stage", "follow_up_date"):
        if field in payload.model_fields_set:
            setattr(lead, field, getattr(payload, field))
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


# ── record-contact: the outreach-event stamp (2026-08-08) ─────────────────
# Called by the inbox after a SUCCESSFUL send to a lead. Semantics are
# deliberately narrower than advance-stage / the status PATCH:
#   - it can only ever move new → contacted, never sideways or backwards
#     (a stale browser tab re-sending must not downgrade a won lead), and
#   - it always refreshes the "last contact" evidence, because a real send
#     to a quoted/won lead is still a contact fact worth recording.
# Idempotent by construction — safe to fire without reading current state.


@router.post(
    "/api/leads/{lead_id}/record-contact",
    response_model=None,
    dependencies=[Depends(require_permission("leads.write"))],
)
def record_lead_contact(
    lead_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    lead = _get_lead_scoped(db, lead_id, tenant_id)
    old_stage = lead.stage
    lead.last_contact_at = datetime.now(timezone.utc)
    if lead.stage == "new":
        lead.stage = "contacted"
    db.commit()
    db.refresh(lead)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="lead_contact_recorded",
        entity_type="lead",
        entity_id=str(lead.id),
        details={"from": old_stage, "to": lead.stage},
        request=request,
    )
    return _serialize_lead(lead)


@router.post(
    "/api/landing-leads/{ll_id}/record-contact",
    response_model=None,
    dependencies=[Depends(require_permission("leads.write"))],
)
def record_landing_contact(
    ll_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    r = _get_landing_scoped(db, ll_id, tenant_id)
    old_status = r.status
    if r.contacted_at is None:
        r.contacted_at = datetime.now(timezone.utc)
    if r.status == "new":
        r.status = "contacted"
    db.commit()
    db.refresh(r)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="landing_lead_contact_recorded",
        entity_type="landing_lead",
        entity_id=str(r.id),
        details={"from": old_status, "to": r.status},
        request=request,
    )
    return _serialize_landing(r)


def _find_matching_customer(db: Session, *, email: str | None, phone: str | None):
    """Existing-customer match for lead conversion.

    Exact email (case-insensitive) first, then separator-stripped phone
    suffix — the customers.py search idiom (chained replace(), portable to
    sqlite tests + Postgres prod). Prevents a repeat web form from the same
    person minting a duplicate customer row. Oldest match wins so repeat
    conversions land on the same record.
    """
    from gdx_dispatch.models.tenant_models import Customer

    not_deleted = (
        Customer.deleted_at.is_(None),
        # Same legacy-data filter as list_customers: old rows carry a
        # "(deleted)" name marker instead of deleted_at.
        ~func.lower(func.coalesce(Customer.name, "")).like("%(deleted)%"),
    )
    email_n = (email or "").strip().lower()
    if email_n:
        row = db.execute(
            select(Customer)
            .where(*not_deleted, func.lower(func.coalesce(Customer.email, "")) == email_n)
            .order_by(Customer.created_at.asc())
            .limit(1)
        ).scalars().first()
        if row:
            return row
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    # ≥10 digits required (unlike the search endpoints' 7): search shows a
    # human a candidate list, this silently LINKS records. A 7-digit number
    # without an area code would match any customer sharing the local part.
    if len(digits) >= 10:
        stripped = Customer.phone
        for sep in (" ", "-", "(", ")", ".", "+"):
            stripped = func.replace(stripped, sep, "")
        # Compare on the last 10 digits so "+1 612 555 0200" matches
        # "(612) 555-0200" regardless of which side carries the country code.
        row = db.execute(
            select(Customer)
            .where(*not_deleted, stripped.like(f"%{digits[-10:]}%"))
            .order_by(Customer.created_at.asc())
            .limit(1)
        ).scalars().first()
        if row:
            return row
    return None


def _resolve_or_create_customer(
    db: Session,
    *,
    lead: Lead,
    tenant_id: str,
    now: datetime,
    user: dict,
    request: Request,
) -> tuple[Any | None, str, str | None]:
    """Resolve customer via existing link, dedupe match, or fresh creation.

    Returns (customer_or_None, status, error_reason).
    status is one of 'existing', 'matched', 'new', 'error'.
    Non-committing: flushes new customer and adds customer_created audit row.
    """
    from gdx_dispatch.models.tenant_models import Customer

    # 1. Existing converted customer?
    if lead.converted_customer_id:
        try:
            prior = db.execute(
                select(Customer).where(
                    Customer.id == lead.converted_customer_id,
                    Customer.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
        except (OperationalError, ProgrammingError):
            db.rollback()
            prior = None
        if prior is not None:
            return prior, "existing", None

    # 2. Dedupe match?
    try:
        match = _find_matching_customer(db, email=lead.email, phone=lead.phone)
    except (OperationalError, ProgrammingError):
        log.exception("convert_dedupe_probe_failed lead_id=%s", lead.id)
        db.rollback()
        match = None
    if match is not None:
        lead.converted_customer_id = match.id
        return match, "matched", None

    # 3. Create fresh customer
    new_customer_id = uuid4()
    try:
        customer = Customer(
            id=new_customer_id,
            company_id=tenant_id,
            name=lead.name or "",
            email=lead.email,
            phone=lead.phone,
            address=lead.address,
            # Customer.source is String(50); Lead.source allows 100.
            source=(lead.source or None) and lead.source[:50],
            created_at=now,
        )
        db.add(customer)
        db.flush()
        lead.converted_customer_id = customer.id
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=_user_id(user),
            action="customer_created",
            entity_type="customer",
            entity_id=str(customer.id),
            details={
                "source": "lead_conversion",
                "lead_id": str(lead.id),
                "name": customer.name,
            },
            request=request,
        )
        return customer, "new", None
    except (OperationalError, ProgrammingError) as exc:
        log.exception("convert_to_customer_insert_failed lead_id=%s", lead.id)
        db.rollback()
        return None, "error", f"customers table unavailable: {type(exc).__name__}"
    except Exception as exc:
        log.exception("convert_to_customer_unexpected lead_id=%s", lead.id)
        db.rollback()
        return None, "error", f"unexpected error: {exc}"


@router.post(
    "/api/leads/{lead_id}/convert-to-customer",
    response_model=None,
    dependencies=[Depends(require_permission("leads.write"))],
)
def convert_to_customer(
    lead_id: UUID,
    request: Request,
    payload: ConvertToCustomerIn | None = None,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    lead = _get_lead_scoped(db, lead_id, tenant_id)
    target_stage = payload.stage if payload else "won"
    now = datetime.now(timezone.utc)

    def _apply_stage() -> None:
        if target_stage == "won" or lead.stage != "won":
            lead.stage = target_stage

    def _finish(customer_id: UUID, *, existing: bool, matched: bool = False, customer_name: str | None = None) -> dict[str, Any]:
        lead.converted_customer_id = customer_id
        if lead.converted_at is None:
            lead.converted_at = now
        _apply_stage()
        db.commit()
        db.refresh(lead)
        _audit(
            db,
            tenant_id=tenant_id,
            user=user,
            action="lead_converted_to_customer",
            entity_type="lead",
            entity_id=str(lead.id),
            details={
                "customer_id": str(customer_id),
                "existing": existing,
                "matched": matched,
                "stage": lead.stage,
            },
            request=request,
        )
        return {
            "lead_id": str(lead.id),
            "customer_id": str(customer_id),
            "converted": True,
            "existing": existing,
            "customer_name": customer_name,
        }

    customer, status, err = _resolve_or_create_customer(
        db, lead=lead, tenant_id=tenant_id, now=now, user=user, request=request
    )
    if customer is None:
        return {
            "lead_id": str(lead.id),
            "customer_id": None,
            "converted": False,
            "reason": err or "customer creation failed",
        }

    return _finish(customer.id, existing=(status in ("existing", "matched")), matched=(status == "matched"), customer_name=customer.name)


@router.post(
    "/api/leads/{lead_id}/start-estimate",
    response_model=None,
    dependencies=[Depends(require_permission("leads.write", "estimates.write"))],
)
def start_estimate(
    lead_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    lead = _get_lead_scoped(db, lead_id, tenant_id)
    now = datetime.now(timezone.utc)

    from gdx_dispatch.models.tenant_models import Customer
    from gdx_dispatch.modules.proposals.models import Estimate

    # Idempotent: return existing draft estimate if already started
    if lead.estimate_id:
        prior = db.execute(
            select(Estimate).where(
                Estimate.id == lead.estimate_id,
                Estimate.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if prior is not None:
            customer = None
            if prior.customer_id:
                customer = db.execute(
                    select(Customer).where(
                        Customer.id == prior.customer_id,
                        Customer.deleted_at.is_(None),
                    )
                ).scalar_one_or_none()
            return {
                "estimate": _serialize_estimate(prior, include_lines=True),
                "customer": {
                    "id": str(customer.id),
                    "name": customer.name,
                    "status": "existing",
                } if customer else None,
                "reused": True,
            }

    customer, status, err = _resolve_or_create_customer(
        db, lead=lead, tenant_id=tenant_id, now=now, user=user, request=request
    )
    if customer is None:
        raise HTTPException(status_code=500, detail=f"Failed to resolve customer: {err}")

    estimate = create_draft_estimate_record(
        db,
        tenant_id=tenant_id,
        customer_id=customer.id,
        jobsite_address=lead.address,
    )
    lead.estimate_id = estimate.id

    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=_user_id(user),
        action="estimate_created",
        entity_type="estimate",
        entity_id=str(estimate.id),
        details={
            "estimate_number": estimate.estimate_number,
            "status": estimate.status,
            "line_count": 0,
            "total": 0.0,
        },
        request=request,
    )
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=_user_id(user),
        action="lead_estimate_started",
        entity_type="lead",
        entity_id=str(lead.id),
        details={
            "estimate_id": str(estimate.id),
            "customer_id": str(customer.id),
            "customer_status": status,
        },
        request=request,
    )

    db.commit()
    db.refresh(lead)
    db.refresh(estimate)

    return {
        "estimate": _serialize_estimate(estimate, include_lines=True),
        "customer": {
            "id": str(customer.id),
            "name": customer.name,
            "status": status,
        },
        "reused": False,
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
