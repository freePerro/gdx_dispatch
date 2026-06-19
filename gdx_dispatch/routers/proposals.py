"""
Proposals router — Good/Better/Best tiered sales proposals.

Pattern mirrors gdx_dispatch/routers/change_orders.py (CRUD + state transitions + audit).
Gated behind the "estimates" module (proposals are sales documents).
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["proposals"],
    dependencies=[Depends(require_module("estimates"))],
)


PROPOSAL_STATUSES = ("draft", "sent", "accepted", "declined")
PROPOSAL_TIERS = ("good", "better", "best")


from gdx_dispatch.models.tenant_models import Proposal  # noqa: E402


class ProposalIn(BaseModel):
    customer_id: str | None = Field(default=None, max_length=64)
    customer_name: str | None = Field(default=None, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10000)
    good_price: float = Field(default=0, ge=0, le=10_000_000)
    better_price: float = Field(default=0, ge=0, le=10_000_000)
    best_price: float = Field(default=0, ge=0, le=10_000_000)
    good_description: str | None = Field(default=None, max_length=5000)
    better_description: str | None = Field(default=None, max_length=5000)
    best_description: str | None = Field(default=None, max_length=5000)


class ProposalPatch(BaseModel):
    customer_id: str | None = Field(default=None, max_length=64)
    customer_name: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10000)
    good_price: float | None = Field(default=None, ge=0, le=10_000_000)
    better_price: float | None = Field(default=None, ge=0, le=10_000_000)
    best_price: float | None = Field(default=None, ge=0, le=10_000_000)
    good_description: str | None = Field(default=None, max_length=5000)
    better_description: str | None = Field(default=None, max_length=5000)
    best_description: str | None = Field(default=None, max_length=5000)


class AcceptTierIn(BaseModel):
    tier: str = Field(pattern=r"^(good|better|best)$")


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


def _serialize(p: Proposal) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "company_id": p.company_id,
        "customer_id": str(p.customer_id) if p.customer_id else None,
        "customer_name": p.customer_name,
        "title": p.title,
        "description": p.description,
        "good_price": float(p.good_price or 0),
        "better_price": float(p.better_price or 0),
        "best_price": float(p.best_price or 0),
        "good_description": p.good_description,
        "better_description": p.better_description,
        "best_description": p.best_description,
        "status": p.status,
        "chosen_tier": p.chosen_tier,
        "sent_at": p.sent_at.isoformat() if p.sent_at else None,
        "accepted_at": p.accepted_at.isoformat() if p.accepted_at else None,
        "created_by": p.created_by,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _get_scoped(db: Session, proposal_id: UUID, tenant_id: str) -> Proposal:
    row = db.execute(
        select(Proposal).where(
            Proposal.id == proposal_id,
            Proposal.company_id == tenant_id,
            Proposal.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return row


def _audit(
    db: Session,
    *,
    tenant_id: str,
    user: Any,
    action: str,
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
            entity_type="proposal",
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("proposal_audit_failed action=%s entity_id=%s", action, entity_id)
        db.rollback()


@router.get("/api/proposals", response_model=None)
def list_proposals(
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
    stmt = select(Proposal).where(
        Proposal.deleted_at.is_(None),
    )
    if status:
        stmt = stmt.where(Proposal.status == status)
    if customer_id:
        try:
            stmt = stmt.where(Proposal.customer_id == UUID(customer_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid customer_id") from None
    stmt = stmt.order_by(Proposal.created_at.desc()).limit(limit).offset(offset)
    rows = db.execute(stmt).scalars().all()
    return [_serialize(r) for r in rows]


@router.post("/api/proposals", response_model=None, status_code=201)
def create_proposal(
    payload: ProposalIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    customer_uuid: UUID | None = None
    if payload.customer_id:
        try:
            customer_uuid = UUID(payload.customer_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid customer_id") from None
    p = Proposal(
        company_id=tenant_id,
        customer_id=customer_uuid,
        customer_name=payload.customer_name,
        title=payload.title.strip(),
        description=payload.description,
        good_price=Decimal(str(payload.good_price)),
        better_price=Decimal(str(payload.better_price)),
        best_price=Decimal(str(payload.best_price)),
        good_description=payload.good_description,
        better_description=payload.better_description,
        best_description=payload.best_description,
        status="draft",
        created_by=_user_id(user),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="proposal_created",
        entity_id=str(p.id),
        details={"title": p.title},
        request=request,
    )
    return _serialize(p)


@router.get("/api/proposals/{proposal_id}", response_model=None)
def get_proposal(
    proposal_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    return _serialize(_get_scoped(db, proposal_id, tenant_id))


@router.patch("/api/proposals/{proposal_id}", response_model=None)
def update_proposal(
    proposal_id: UUID,
    payload: ProposalPatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    p = _get_scoped(db, proposal_id, tenant_id)
    if p.status != "draft":
        raise HTTPException(
            status_code=400,
            detail="Proposal can only be edited while in draft status",
        )
    data = payload.model_dump(exclude_unset=True)
    if "customer_id" in data:
        cid = data.pop("customer_id")
        if cid:
            try:
                p.customer_id = UUID(cid)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid customer_id") from None
        else:
            p.customer_id = None
    for field in (
        "customer_name",
        "title",
        "description",
        "good_description",
        "better_description",
        "best_description",
    ):
        if field in data:
            setattr(p, field, data[field])
    for field in ("good_price", "better_price", "best_price"):
        if field in data and data[field] is not None:
            setattr(p, field, Decimal(str(data[field])))
    db.commit()
    db.refresh(p)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="proposal_updated",
        entity_id=str(p.id),
        details={"fields": list(data.keys())},
        request=request,
    )
    return _serialize(p)


@router.post("/api/proposals/{proposal_id}/send", response_model=None)
def send_proposal(
    proposal_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    p = _get_scoped(db, proposal_id, tenant_id)
    if p.status not in ("draft", "sent"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot send proposal in status '{p.status}'",
        )
    p.status = "sent"
    p.sent_at = utcnow()
    db.commit()
    db.refresh(p)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="proposal_sent",
        entity_id=str(p.id),
        request=request,
    )
    return _serialize(p)


@router.post("/api/proposals/{proposal_id}/accept", response_model=None)
def accept_proposal(
    proposal_id: UUID,
    payload: AcceptTierIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    p = _get_scoped(db, proposal_id, tenant_id)
    if p.status == "declined":
        raise HTTPException(status_code=400, detail="Cannot accept a declined proposal")
    p.status = "accepted"
    p.chosen_tier = payload.tier
    p.accepted_at = utcnow()
    db.commit()
    db.refresh(p)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="proposal_accepted",
        entity_id=str(p.id),
        details={"tier": payload.tier},
        request=request,
    )
    return _serialize(p)


@router.post("/api/proposals/{proposal_id}/decline", response_model=None)
def decline_proposal(
    proposal_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    p = _get_scoped(db, proposal_id, tenant_id)
    if p.status == "accepted":
        raise HTTPException(status_code=400, detail="Cannot decline an accepted proposal")
    p.status = "declined"
    db.commit()
    db.refresh(p)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="proposal_declined",
        entity_id=str(p.id),
        request=request,
    )
    return _serialize(p)


@router.delete("/api/proposals/{proposal_id}", response_model=None, status_code=204)
def delete_proposal(
    proposal_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    p = _get_scoped(db, proposal_id, tenant_id)
    p.deleted_at = utcnow()
    db.commit()
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="proposal_deleted",
        entity_id=str(proposal_id),
        request=request,
    )
    return None
