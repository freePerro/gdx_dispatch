"""Portal quote requests — customers ask for new doors; the office gets a Lead.

`portal_router` (/portal/quote-requests) is the customer side, behind the
portal JWT and scoped to the signed-in customer on every read and write: a
guessed id answers 404. `router` (/api/quote-requests) is the staff side,
`leads.read`, under the `customers` module like the Leads router — the request
is shown on the lead it opened and on the estimate started from that lead.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from gdx_dispatch.core.audit import audit_ready_db, log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.core.office_notifications import notify_office
from gdx_dispatch.core.pay_periods import resolve_zone, shop_tz_name_from_settings
from gdx_dispatch.core.upload_limits import assert_upload_within_limit
from gdx_dispatch.models.tenant_models import Customer, Lead
from gdx_dispatch.modules.quote_requests import service
from gdx_dispatch.modules.quote_requests.models import DOORS_VERSION, QuoteRequest, QuoteRequestPhoto
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.leads import CLOSED_STAGES, _progress_for_leads
from gdx_dispatch.routers.portal import PortalPrincipal, get_current_portal_customer

log = logging.getLogger(__name__)

portal_router = APIRouter(
    prefix="/portal/quote-requests",
    tags=["customer_portal"],
    dependencies=[Depends(require_module("customer_portal"))],
)
router = APIRouter(
    prefix="/api/quote-requests",
    tags=["quote_requests"],
    dependencies=[Depends(require_module("customers"))],
)


def _tenant_id(request: Request) -> str:
    return str(getattr(request.state, "tenant", {}).get("id", ""))


def _statuses(db: Session, reqs: list[QuoteRequest]) -> dict[str, str]:
    """{request_id: customer-facing status}, from the leads' estimates."""
    lead_ids = [r.lead_id for r in reqs if r.lead_id is not None]
    leads = (
        db.execute(select(Lead).where(Lead.id.in_(lead_ids), Lead.deleted_at.is_(None))).scalars().all()
        if lead_ids else []
    )
    by_id = {lead.id: lead for lead in leads}
    progress = _progress_for_leads(db, list(leads)) if leads else {}
    return {
        # A lead staff deleted (spam, a duplicate) closes its request; read
        # as "Received" it stayed editable and rang the bell for a dead lead.
        str(r.id): "Closed" if r.withdrawn_at is None and r.lead_id is not None and r.lead_id not in by_id
        else service.customer_status(
            by_id.get(r.lead_id), progress.get(str(r.lead_id)), withdrawn=r.withdrawn_at is not None
        )
        for r in reqs
    }


def _own_request_or_404(db: Session, request_id: UUID, principal: PortalPrincipal) -> QuoteRequest:
    req = db.execute(
        select(QuoteRequest)
        .options(selectinload(QuoteRequest.photos))
        .where(
            QuoteRequest.id == request_id,
            QuoteRequest.customer_id == principal.customer_id,
            QuoteRequest.deleted_at.is_(None),
        )
    ).scalars().first()
    if req is None:
        raise HTTPException(status_code=404, detail="Not found")
    return req


# ── Customer side ────────────────────────────────────────────────────────────


@portal_router.get("", response_model=None)
def portal_list_quote_requests(
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """The customer's own requests, newest first, each with where it stands."""
    reqs = db.execute(
        select(QuoteRequest)
        .options(selectinload(QuoteRequest.photos))
        .where(
            QuoteRequest.customer_id == principal.customer_id,
            QuoteRequest.deleted_at.is_(None),
        )
        .order_by(QuoteRequest.created_at.desc())
    ).scalars().all()
    statuses = _statuses(db, list(reqs))
    return [service.serialize(r, status=statuses[str(r.id)]) for r in reqs]


@portal_router.post("", response_model=None, status_code=201)
def portal_submit_quote_request(
    payload: service.QuoteRequestIn,
    request: Request,
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(audit_ready_db),
) -> dict[str, Any]:
    """Record the request and open a Lead for it in one transaction.

    The lead is already the customer's (`converted_customer_id`), so the
    office's Start Estimate reuses this customer instead of creating one.
    """
    customer = db.execute(
        select(Customer).where(Customer.id == principal.customer_id, Customer.deleted_at.is_(None))
    ).scalar_one_or_none()
    if customer is None:
        raise HTTPException(status_code=404, detail="Not found")

    tenant_id = _tenant_id(request)
    now = datetime.now(UTC)
    req = QuoteRequest(
        customer_id=customer.id,
        submitted_by_user_id=principal.user_id,
        job_name=payload.job_name,
        site_address=payload.site_address,
        notes=payload.notes,
        doors=[d.model_dump() for d in payload.doors],
        doors_version=DOORS_VERSION,
        created_at=now,
    )
    db.add(req)
    db.flush()

    actor = f"portal:{principal.user_id}"
    address = payload.site_address or customer.address
    lead = Lead(
        company_id=tenant_id,
        name=customer.name,
        email=customer.email,
        phone=customer.phone,
        address=address[:500] if address else None,
        stage="new",
        estimated_value=0,
        source="Customer portal",
        notes=service.lead_notes(req),
        origin_ref=service.origin_ref(req),
        converted_customer_id=customer.id,
        converted_at=now,
        created_by=actor,
        created_at=now,
        updated_at=now,
    )
    db.add(lead)
    db.flush()
    req.lead_id = lead.id

    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=actor,
        action="quote_request_submitted",
        entity_type="quote_request",
        entity_id=str(req.id),
        details={
            "customer_id": str(customer.id),
            "lead_id": str(lead.id),
            "job_name": req.job_name,
            "door_count": len(req.doors),
        },
        request=request,
    )
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=actor,
        action="lead_created",
        entity_type="lead",
        entity_id=str(lead.id),
        details={"stage": lead.stage, "source": lead.source, "quote_request_id": str(req.id)},
        request=request,
    )
    db.commit()
    db.refresh(req)

    door_count = service.door_count(req)
    notify_office(
        db,
        tenant_id,
        title=f"Quote request from {customer.name}",
        message=f"{req.job_name}: {door_count} door{'s' if door_count != 1 else ''}, from the customer portal.",
        category="lead",
    )
    return service.serialize(req, status="Received")


@portal_router.post("/{request_id}/photos", response_model=None, status_code=201)
async def portal_upload_quote_request_photo(
    request_id: UUID,
    request: Request,
    door_index: int = Form(...),
    file: UploadFile = File(...),
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(audit_ready_db),
) -> dict[str, Any]:
    """Add a photo to one door. No status window: a photo that lands after the
    quote went out is still useful, and a failed upload on a phone has to be
    retryable later (plan audit finding 3)."""
    req = _own_request_or_404(db, request_id, principal)
    if req.withdrawn_at is not None:
        raise HTTPException(status_code=409, detail="This request was withdrawn")
    if not 0 <= door_index < len(req.doors or []):
        raise HTTPException(status_code=422, detail="No such door on this request")
    if len(service.photos_for_door(req, door_index)) >= service.MAX_PHOTOS_PER_DOOR:
        raise HTTPException(
            status_code=422, detail=f"At most {service.MAX_PHOTOS_PER_DOOR} photos per door"
        )

    content_type = (file.content_type or "").lower().split(";")[0].strip()
    if content_type not in service.ALLOWED_PHOTO_MIME_TYPES:
        raise HTTPException(status_code=415, detail="Use a JPEG, PNG or WebP photo")
    assert_upload_within_limit(file, service.MAX_PHOTO_BYTES)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > service.MAX_PHOTO_BYTES:
        raise HTTPException(status_code=413, detail="Photo too large")

    tenant_id = _tenant_id(request)
    try:
        photo = service.store_photo(
            db, tenant_id=tenant_id, req=req, door_index=door_index, data=data, content_type=content_type
        )
    except service.PhotoProcessingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.flush()
    log_audit_event_sync(
        db,
        tenant_id=tenant_id,
        user_id=f"portal:{principal.user_id}",
        action="quote_request_photo_added",
        entity_type="quote_request",
        entity_id=str(req.id),
        details={"photo_id": str(photo.id), "door_index": door_index},
        request=request,
    )
    db.commit()
    return {"id": str(photo.id), "door_index": door_index}


def _customer_name(db: Session, customer_id: UUID) -> str:
    name = db.execute(select(Customer.name).where(Customer.id == customer_id)).scalar_one_or_none()
    return name or "a customer"


def _stamp(db: Session, dt: datetime) -> str:
    """When it happened, in the shop's own zone. The bell shows a row's age
    ("5m ago", then only the date), so the exact time lives in the message;
    the containers run on UTC, which would read hours off to the office."""
    return dt.astimezone(resolve_zone(shop_tz_name_from_settings(db))).strftime("%b %-d, %Y %-I:%M %p")


def _own_request_in(
    db: Session, request_id: UUID, principal: PortalPrincipal, allowed: frozenset[str], verb: str
) -> tuple[QuoteRequest, str]:
    """The customer's own request and its status, or 409 when the status is
    outside the window the action is allowed in."""
    from gdx_dispatch.modules.proposals.models import Estimate

    req = _own_request_or_404(db, request_id, principal)
    status = _statuses(db, [req])[str(req.id)]
    if status == "Received" and req.lead_id is not None and db.execute(
        select(Estimate.id).where(Estimate.lead_id == req.lead_id, Estimate.deleted_at.is_(None)).limit(1)
    ).first() is not None:
        # The lead has estimates but no progress came back: the progress
        # helper degrades to "nothing known" on a read error, and for a gate
        # unknown is no.
        status = "status unavailable"
    if status not in allowed:
        raise HTTPException(status_code=409, detail=f"This request can no longer be {verb} ({status})")
    return req, status


def _audit_portal_action(
    db: Session, request: Request, req: QuoteRequest, actor: str, action: str, details: dict[str, Any]
) -> None:
    log_audit_event_sync(
        db,
        tenant_id=_tenant_id(request),
        user_id=actor,
        action=action,
        entity_type="quote_request",
        entity_id=str(req.id),
        details={"lead_id": str(req.lead_id) if req.lead_id else None, **details},
        request=request,
    )


def _live_lead(db: Session, req: QuoteRequest) -> Lead | None:
    lead = db.get(Lead, req.lead_id) if req.lead_id else None
    return lead if lead is not None and lead.deleted_at is None else None


@portal_router.patch("/{request_id}", response_model=None)
def portal_edit_quote_request(
    request_id: UUID,
    payload: service.QuoteRequestEdit,
    request: Request,
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(audit_ready_db),
) -> dict[str, Any]:
    """Change a request until a quote has been sent. The office is alerted and
    the audit row carries each changed field before and after."""
    req, status = _own_request_in(db, request_id, principal, service.EDITABLE_STATUSES, "changed")

    now = datetime.now(UTC)
    old_notes = service.lead_notes(req)
    try:
        changes = service.apply_edit(req, payload, now=now)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not changes:
        return service.serialize(req, status=status)

    req.edited_at = now
    req.updated_at = now
    tenant_id = _tenant_id(request)
    actor = f"portal:{principal.user_id}"
    lead = _live_lead(db, req)
    if lead is not None and lead.notes == old_notes:
        # Only the line this code wrote; notes staff have touched stay theirs.
        lead.notes = service.lead_notes(req)
        lead.updated_at = now
    db.flush()
    _audit_portal_action(db, request, req, actor, "quote_request_edited", {"changes": changes})
    db.commit()
    db.refresh(req)

    labels = {"job_name": "job name", "site_address": "address", "notes": "notes", "doors": "doors",
              "removed_photo_ids": "photos"}
    changed = ", ".join(labels[k] for k in changes)
    notify_office(
        db,
        tenant_id,
        title=f"Quote request edited by {_customer_name(db, req.customer_id)}",
        message=f"{req.job_name}: changed {changed} at {_stamp(db, now)}, from the customer portal.",
        category="lead",
    )
    return service.serialize(req, status=status)


@portal_router.post("/{request_id}/withdraw", response_model=None)
def portal_withdraw_quote_request(
    request_id: UUID,
    request: Request,
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(audit_ready_db),
) -> dict[str, Any]:
    """Withdraw a request the customer has not accepted a quote on. The request
    stays on record, stamped; its lead moves to lost so the office stops
    working it. An estimate already started is left as it is."""
    req, status = _own_request_in(db, request_id, principal, service.WITHDRAWABLE_STATUSES, "withdrawn")

    now = datetime.now(UTC)
    req.withdrawn_at = now
    req.updated_at = now
    tenant_id = _tenant_id(request)
    actor = f"portal:{principal.user_id}"
    lead = _live_lead(db, req)
    lead_stage_from = None
    if lead is not None and lead.stage not in CLOSED_STAGES:
        lead_stage_from = lead.stage
        lead.stage = "lost"
        lead.updated_at = now
    db.flush()
    _audit_portal_action(db, request, req, actor, "quote_request_withdrawn", {"status_before": status})
    if lead_stage_from is not None:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=actor,
            action="lead_updated",
            entity_type="lead",
            entity_id=str(lead.id),
            details={
                "stage": {"from": lead_stage_from, "to": "lost"},
                "reason": "quote request withdrawn by the customer",
                "quote_request_id": str(req.id),
            },
            request=request,
        )
    db.commit()
    db.refresh(req)

    notify_office(
        db,
        tenant_id,
        title=f"Quote request withdrawn by {_customer_name(db, req.customer_id)}",
        message=(
            f"{req.job_name}: withdrawn at {_stamp(db, now)}, from the customer portal."
            + (" Its lead is now lost." if lead_stage_from is not None else "")
        ),
        category="lead",
    )
    return service.serialize(req, status=service.WITHDRAWN)


# ── Staff side ───────────────────────────────────────────────────────────────


@router.get(
    "/by-lead/{lead_id}",
    response_model=None,
    dependencies=[Depends(require_permission("leads.read"))],
)
def get_quote_request_for_lead(
    lead_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any] | None:
    """The request a lead came from, or null for a lead that has none."""
    req = db.execute(
        select(QuoteRequest)
        .options(selectinload(QuoteRequest.photos))
        .where(QuoteRequest.lead_id == lead_id, QuoteRequest.deleted_at.is_(None))
    ).scalars().first()
    if req is None:
        return None
    return service.serialize(req, status=_statuses(db, [req])[str(req.id)], lead_id_visible=True)


@router.get(
    "/{request_id}/photos/{photo_id}",
    response_model=None,
    dependencies=[Depends(require_permission("leads.read"))],
)
def get_quote_request_photo(
    request_id: UUID,
    photo_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    photo = db.execute(
        select(QuoteRequestPhoto).where(
            QuoteRequestPhoto.id == photo_id,
            QuoteRequestPhoto.quote_request_id == request_id,
            QuoteRequestPhoto.deleted_at.is_(None),
        )
    ).scalars().first()
    if photo is None:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        path = service.photo_path(_tenant_id(request), request_id, photo.filename)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type=photo.content_type)
