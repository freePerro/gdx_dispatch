"""Marketing campaigns router — CRUD + send trigger.

Closes the Vue CampaignsView.vue → backend wiring gap surfaced by rd_operations.py.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy import text as _text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.tenant import company_id
from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import MarketingCampaign

log = logging.getLogger(__name__)

try:
    from gdx_dispatch.routers.auth import get_current_user
except ImportError:  # fallback provided to allow module loading if auth module is missing
    log.exception("campaigns_auth_import_failed_using_fallback")
    async def get_current_user() -> dict[str, Any]:
        return {}

router = APIRouter(
    prefix="/api/campaigns",
    tags=["campaigns"],
    dependencies=[Depends(require_module("campaigns"))],
)


def jsonable_response(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=jsonable_encoder(content))


_VALID_TYPES = {"email", "sms"}
_VALID_STATUSES = {"draft", "active", "paused", "completed", "sending"}
_TABLE_ENSURED = False


class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    # type is enum-ish (email/sms/push); status is draft/sent/scheduled
    type: str = Field(default="email", min_length=1, max_length=50)
    status: str = Field(default="draft", min_length=1, max_length=50)
    subject: str | None = Field(default=None, max_length=500)
    # Campaign bodies can be long HTML but capped at 64KB to prevent DoS
    body: str | None = Field(default=None, max_length=65_536)
    audience: str | None = Field(default=None, max_length=64)
    scheduled_at: datetime | None = None


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    type: str | None = Field(default=None, min_length=1, max_length=50)
    status: str | None = Field(default=None, min_length=1, max_length=50)
    subject: str | None = Field(default=None, max_length=500)
    body: str | None = Field(default=None, max_length=65_536)
    audience: str | None = Field(default=None, max_length=64)
    scheduled_at: datetime | None = None


def _tenant_id(request: Request) -> str:
    return str(getattr(request.state, "tenant", {}).get("id", ""))


def _user_id(current_user: Any) -> str:
    return str((current_user or {}).get("sub") or (current_user or {}).get("user_id") or "system")


def _campaign_to_dict(c: MarketingCampaign) -> dict[str, Any]:
    """Serialize a MarketingCampaign ORM instance to a dict matching the API contract."""
    return {
        "id": str(c.id),
        "company_id": c.company_id,
        "name": c.name,
        "type": c.type,
        "status": c.status,
        "subject": c.subject,
        "body": c.body,
        "audience": c.audience,
        "scheduled_at": c.scheduled_at,
        "last_sent_at": c.last_sent_at,
        "sent_count": c.sent_count,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


@router.get("", response_model=None)
def list_campaigns(
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
    status: str | None = None,
    type: str | None = None,
):
    _ = current_user
    tenant_id = _tenant_id(request)
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    offset = (page - 1) * page_size

    try:
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        filters = [
            MarketingCampaign.deleted_at.is_(None),
        ]
        if search:
            like_pattern = f"%{search}%"
            filters.append(
                (MarketingCampaign.name.ilike(like_pattern))
                | (MarketingCampaign.subject.ilike(like_pattern))
            )
        if status:
            filters.append(MarketingCampaign.status == status)
        if type:
            filters.append(MarketingCampaign.type == type)

        total = db.execute(
            select(func.count()).select_from(MarketingCampaign).where(*filters)
        ).scalar() or 0

        rows = db.execute(
            select(MarketingCampaign)
            .where(*filters)
            .order_by(MarketingCampaign.created_at.desc())
            .limit(page_size)
            .offset(offset)
        ).scalars().all()
    except SQLAlchemyError as exc:
        log.exception("list_campaigns_failed", extra={"tenant_id": tenant_id})
        return jsonable_response({"detail": f"Database error: {exc}"}, 500)

    return jsonable_response({
        "items": [_campaign_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.post("", response_model=None)
def create_campaign(
    payload: CampaignCreate,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    if payload.type not in _VALID_TYPES:
        return jsonable_response(
            {"detail": f"type must be one of {sorted(_VALID_TYPES)}"}, 400,
        )
    if payload.status not in _VALID_STATUSES:
        return jsonable_response(
            {"detail": f"status must be one of {sorted(_VALID_STATUSES)}"}, 400,
        )

    now = datetime.now(UTC)
    campaign_id = str(uuid.uuid4())
    try:
        campaign = MarketingCampaign(
            id=campaign_id,
            company_id=tenant_id,
            name=payload.name.strip(),
            type=payload.type,
            status=payload.status,
            subject=payload.subject,
            body=payload.body,
            audience=payload.audience,
            scheduled_at=payload.scheduled_at,
            sent_count=0,
            created_at=now,
            updated_at=now,
        )
        db.add(campaign)
        db.flush()
        result = _campaign_to_dict(campaign)
        db.commit()
        log_audit_event_sync(
            db=db,
            tenant_id=tenant_id,
            user_id=_user_id(current_user),
            action="campaign_created",
            entity_type="campaign",
            entity_id=campaign_id,
            details={"name": payload.name, "type": payload.type, "status": payload.status},
            ip_address=request.client.host if request.client else None,
            request=request,
        )
        db.commit()
        log.info("campaign_created", extra={"tenant_id": tenant_id, "campaign_id": campaign_id})
        return jsonable_response(result, 201)
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("create_campaign_failed", extra={"tenant_id": tenant_id})
        return jsonable_response({"detail": f"Database error: {exc}"}, 500)


def _validate_uuid(campaign_id: str) -> bool:
    try:
        uuid.UUID(campaign_id)
        return True
    except (ValueError, AttributeError):  # Validation failure is expected and handled by returning False.
        log.exception("_validate_uuid_failed")
        return False


def _get_campaign(db: Session, campaign_id: str, tenant_id: str) -> MarketingCampaign | None:
    """Fetch a single non-deleted campaign by id and tenant."""
    return db.execute(
        select(MarketingCampaign).where(
            MarketingCampaign.id == campaign_id,
            MarketingCampaign.company_id == tenant_id,
            MarketingCampaign.deleted_at.is_(None),
        )
    ).scalar_one_or_none()


@router.get("/{campaign_id}", response_model=None)
def get_campaign(
    campaign_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ = current_user
    if not _validate_uuid(campaign_id):
        return jsonable_response({"detail": "campaign not found"}, 404)
    tenant_id = _tenant_id(request)
    try:
        campaign = _get_campaign(db, campaign_id, tenant_id)
        if not campaign:
            return jsonable_response({"detail": "campaign not found"}, 404)
        return jsonable_response(_campaign_to_dict(campaign))
    except SQLAlchemyError as exc:
        log.exception("get_campaign_failed", extra={"tenant_id": tenant_id, "campaign_id": campaign_id})
        return jsonable_response({"detail": f"Database error: {exc}"}, 500)


@router.patch("/{campaign_id}", response_model=None)
def update_campaign(
    campaign_id: str,
    payload: CampaignUpdate,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _validate_uuid(campaign_id):
        return jsonable_response({"detail": "campaign not found"}, 404)
    tenant_id = _tenant_id(request)

    data = payload.model_dump(exclude_unset=True) if hasattr(payload, "model_dump") else payload.dict(exclude_unset=True)
    if not data:
        return jsonable_response({"detail": "no fields to update"}, 400)
    if "type" in data and data["type"] not in _VALID_TYPES:
        return jsonable_response(
            {"detail": f"type must be one of {sorted(_VALID_TYPES)}"}, 400,
        )
    if "status" in data and data["status"] not in _VALID_STATUSES:
        return jsonable_response(
            {"detail": f"status must be one of {sorted(_VALID_STATUSES)}"}, 400,
        )
    if "name" in data and data["name"]:
        data["name"] = str(data["name"]).strip()[:255]

    now = datetime.now(UTC)
    data["updated_at"] = now

    try:
        campaign = _get_campaign(db, campaign_id, tenant_id)
        if not campaign:
            return jsonable_response({"detail": "campaign not found"}, 404)
        for key, value in data.items():
            setattr(campaign, key, value)
        db.flush()
        result = _campaign_to_dict(campaign)
        db.commit()
        log_audit_event_sync(
            db=db,
            tenant_id=tenant_id,
            user_id=_user_id(current_user),
            action="campaign_updated",
            entity_type="campaign",
            entity_id=campaign_id,
            details={"fields": [k for k in data if k != "updated_at"]},
            ip_address=request.client.host if request.client else None,
            request=request,
        )
        db.commit()
        log.info("campaign_updated", extra={"tenant_id": tenant_id, "campaign_id": campaign_id})
        return jsonable_response(result)
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("update_campaign_failed", extra={"tenant_id": tenant_id, "campaign_id": campaign_id})
        return jsonable_response({"detail": f"Database error: {exc}"}, 500)


@router.delete("/{campaign_id}", response_model=None)
def delete_campaign(
    campaign_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _validate_uuid(campaign_id):
        return jsonable_response({"detail": "campaign not found"}, 404)
    tenant_id = _tenant_id(request)
    now = datetime.now(UTC)
    try:
        campaign = _get_campaign(db, campaign_id, tenant_id)
        if not campaign:
            return jsonable_response({"detail": "campaign not found"}, 404)
        campaign_name = campaign.name
        campaign.deleted_at = now
        campaign.updated_at = now
        db.flush()
        db.commit()
        log_audit_event_sync(
            db=db,
            tenant_id=tenant_id,
            user_id=_user_id(current_user),
            action="campaign_deleted",
            entity_type="campaign",
            entity_id=campaign_id,
            details={"name": campaign_name},
            ip_address=request.client.host if request.client else None,
            request=request,
        )
        db.commit()
        log.info("campaign_deleted", extra={"tenant_id": tenant_id, "campaign_id": campaign_id})
        return jsonable_response({"ok": True, "id": campaign_id})
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("delete_campaign_failed", extra={"tenant_id": tenant_id, "campaign_id": campaign_id})
        return jsonable_response({"detail": f"Database error: {exc}"}, 500)


@router.post("/{campaign_id}/send", response_model=None)
def send_campaign(
    campaign_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Trigger send. Marks status='sending' and bumps sent_count. Actual
    delivery is handled by a downstream worker (Celery)."""
    if not _validate_uuid(campaign_id):
        return jsonable_response({"detail": "campaign not found"}, 404)
    tenant_id = _tenant_id(request)
    now = datetime.now(UTC)
    try:
        campaign = _get_campaign(db, campaign_id, tenant_id)
        if not campaign:
            return jsonable_response({"detail": "campaign not found"}, 404)
        if campaign.status in ("sending", "completed"):
            return jsonable_response(
                {"detail": f"campaign is {campaign.status}; cannot re-send"}, 409,
            )
        channel = campaign.type
        audience = campaign.audience
        campaign.status = "sending"
        campaign.last_sent_at = now
        campaign.sent_count = (campaign.sent_count or 0) + 1
        campaign.updated_at = now
        db.flush()
        result = {
            "id": str(campaign.id),
            "status": campaign.status,
            "sent_count": campaign.sent_count,
            "last_sent_at": campaign.last_sent_at,
            "queued_at": now,
        }
        db.commit()
        log_audit_event_sync(
            db=db,
            tenant_id=tenant_id,
            user_id=_user_id(current_user),
            action="campaign_send_triggered",
            entity_type="campaign",
            entity_id=campaign_id,
            details={"channel": channel, "audience": audience},
            ip_address=request.client.host if request.client else None,
            request=request,
        )
        db.commit()
        log.info(
            "campaign_send_triggered",
            extra={"tenant_id": tenant_id, "campaign_id": campaign_id, "channel": channel},
        )
        return jsonable_response(result)
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("send_campaign_failed", extra={"tenant_id": tenant_id, "campaign_id": campaign_id})
        return jsonable_response({"detail": f"Database error: {exc}"}, 500)


# --- Sub-resource endpoints (Gemma-generated) ---
# NOTE: _audience_filter, campaign_preview_filter, campaign_sends_list use cross-table
# raw SQL (customers, jobs, campaign_sends) — no ORM models available for those tables.


def _audience_filter(audience: str, tenant_id: str):
    """Return (where_clause, params) for audience targeting SQL."""
    base = "FROM customers WHERE company_id = :tid AND deleted_at IS NULL"
    params: dict = {"tid": tenant_id}
    now = datetime.now(UTC)
    if audience == "active_customers":
        cutoff = now - timedelta(days=90)
        return f"{base} AND id IN (SELECT customer_id FROM jobs WHERE created_at > :cutoff AND deleted_at IS NULL)", {**params, "cutoff": cutoff}
    elif audience == "inactive_30d":
        cutoff = now - timedelta(days=30)
        return f"{base} AND id NOT IN (SELECT customer_id FROM jobs WHERE created_at > :cutoff AND deleted_at IS NULL)", {**params, "cutoff": cutoff}
    elif audience == "inactive_90d":
        cutoff = now - timedelta(days=90)
        return f"{base} AND id NOT IN (SELECT customer_id FROM jobs WHERE created_at > :cutoff AND deleted_at IS NULL)", {**params, "cutoff": cutoff}
    elif audience == "new_leads":
        cutoff = now - timedelta(days=30)
        return f"{base} AND created_at > :cutoff", {**params, "cutoff": cutoff}
    return base, params


@router.post("/preview-filter")
def campaign_preview_filter(request: Request, payload: dict, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant_id = company_id()
    audience = payload.get("target_audience") or "all_customers"
    where, params = _audience_filter(audience, tenant_id)
    count_res = db.execute(_text(f"SELECT COUNT(*) as cnt {where}"), params).mappings().first()
    samples = db.execute(_text(f"SELECT id, name, email {where} LIMIT 5"), params).mappings().all()
    return {"matching_customers": int((count_res or {}).get("cnt", 0)), "sample": [dict(s) for s in samples]}


@router.get("/{campaign_id}/preview")
def campaign_preview(campaign_id: str, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant_id = company_id()
    try:
        campaign = _get_campaign(db, campaign_id, tenant_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found")
        where, params = _audience_filter(campaign.audience or "all_customers", tenant_id)
        cnt = db.execute(_text(f"SELECT COUNT(*) as cnt {where}"), params).mappings().first()
        return {"subject": campaign.subject, "body": campaign.body, "channel": campaign.type, "recipient_count": int((cnt or {}).get("cnt", 0))}
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        log.exception("campaign_preview_failed", extra={"tenant_id": tenant_id, "campaign_id": campaign_id})
        raise HTTPException(500, f"Database error: {exc}") from exc


@router.get("/{campaign_id}/sends")
def campaign_sends_list(campaign_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    offset = (page - 1) * page_size
    total = db.execute(_text("SELECT COUNT(*) as cnt FROM campaign_sends WHERE campaign_id = :cid"), {"cid": campaign_id}).mappings().first()
    items = db.execute(
        _text("SELECT id, customer_name, customer_email, channel, status, sent_at, opened_at, created_at FROM campaign_sends WHERE campaign_id = :cid ORDER BY created_at DESC LIMIT :lim OFFSET :off"),
        {"cid": campaign_id, "lim": page_size, "off": offset},
    ).mappings().all()
    return {"items": [dict(r) for r in items], "total": int((total or {}).get("cnt", 0))}


@router.put("/{campaign_id}/activate")
def activate_campaign(campaign_id: str, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant_id = company_id()
    user_id = user.get("sub") or user.get("user_id") or "system"
    now = datetime.now(UTC)
    try:
        campaign = _get_campaign(db, campaign_id, tenant_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found")
        campaign.status = "active"
        campaign.updated_at = now
        db.commit()
        log_audit_event_sync(db, tenant_id=tenant_id, user_id=user_id, action="activate_campaign", entity_type="campaign", entity_id=campaign_id, details={"status": "active"}, request=request)
        return {"ok": True, "status": "active"}
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("activate_campaign_failed", extra={"tenant_id": tenant_id, "campaign_id": campaign_id})
        raise HTTPException(500, f"Database error: {exc}") from exc


@router.put("/{campaign_id}/deactivate")
def deactivate_campaign(campaign_id: str, request: Request, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant_id = company_id()
    user_id = user.get("sub") or user.get("user_id") or "system"
    now = datetime.now(UTC)
    try:
        campaign = _get_campaign(db, campaign_id, tenant_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found")
        campaign.status = "paused"
        campaign.updated_at = now
        db.commit()
        log_audit_event_sync(db, tenant_id=tenant_id, user_id=user_id, action="deactivate_campaign", entity_type="campaign", entity_id=campaign_id, details={"status": "paused"}, request=request)
        return {"ok": True, "status": "paused"}
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("deactivate_campaign_failed", extra={"tenant_id": tenant_id, "campaign_id": campaign_id})
        raise HTTPException(500, f"Database error: {exc}") from exc
