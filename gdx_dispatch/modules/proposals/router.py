from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import resolve_audit_actor
from gdx_dispatch.core.customer_views import record_customer_view
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.permissions import is_dispatch_manager
from gdx_dispatch.modules.proposals.models import Estimate, ProposalTier
from gdx_dispatch.modules.proposals.service import (
    accept_tier,
    add_proposal_tier,
    delete_proposal_tier,
    update_proposal_tier,
)
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(prefix="/api", tags=["proposals"])


def _require_dispatch(user: dict = Depends(get_current_user)) -> dict:
    """Accepting a proposal tier is a financial commitment — dispatch/admin only."""
    if not is_dispatch_manager(user):
        raise HTTPException(status_code=403, detail="dispatcher or admin role required")
    return user

# tier_name is the DB Enum("good","better","best"). Typed as a bare `str` it
# reached the driver unvalidated and any other value came back as a 500 from
# psycopg rather than a 422 — Literal moves the rejection to the request edge.
# The bounds mirror the deleted /api/proposals router's Field limits.
TierName = Literal["good", "better", "best"]


class TierIn(BaseModel): tier_name: TierName; description: str | None = Field(default=None, max_length=5000); total_price: float = Field(default=0, ge=0, le=10_000_000); warranty_months: int = Field(default=0, ge=0, le=600)  # noqa: E701,E702
class TierPatch(BaseModel): tier_name: TierName | None = None; description: str | None = Field(default=None, max_length=5000); total_price: float | None = Field(default=None, ge=0, le=10_000_000); warranty_months: int | None = Field(default=None, ge=0, le=600); includes_parts: bool | None = None  # noqa: E701,E702
class AcceptIn(BaseModel): tier_id: UUID  # noqa: E701,E702

@router.get("/estimates/{estimate_id}/proposal", response_model=None)
def get_proposal(estimate_id: UUID, _: None = Depends(require_module("proposals")), current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> list[ProposalTier]:
    if not db.execute(select(Estimate.id).where(Estimate.id == estimate_id)).scalar_one_or_none(): raise HTTPException(status_code=404, detail="Estimate not found")  # noqa: E701,E702
    return list(db.execute(select(ProposalTier).where(ProposalTier.estimate_id == estimate_id).order_by(ProposalTier.display_order.asc(), ProposalTier.id.asc())).scalars().all())

@router.post("/estimates/{estimate_id}/proposal-tiers", response_model=None)
def post_proposal_tier(estimate_id: UUID, payload: TierIn, _: None = Depends(require_module("proposals")), current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> ProposalTier:
    return add_proposal_tier(estimate_id, payload.tier_name, payload.description, payload.total_price, payload.warranty_months, db, actor=resolve_audit_actor(current_user))

@router.patch("/estimates/{estimate_id}/proposal-tiers/{tier_id}", response_model=None)
def patch_proposal_tier(estimate_id: UUID, tier_id: UUID, payload: TierPatch, _: None = Depends(require_module("proposals")), current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> ProposalTier:
    return update_proposal_tier(estimate_id, tier_id, payload.model_dump(exclude_unset=True), db, actor=resolve_audit_actor(current_user))

@router.delete("/estimates/{estimate_id}/proposal-tiers/{tier_id}", status_code=204)
def del_proposal_tier(estimate_id: UUID, tier_id: UUID, _: None = Depends(require_module("proposals")), current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    delete_proposal_tier(estimate_id, tier_id, db, actor=resolve_audit_actor(current_user))
    return Response(status_code=204)

@router.post("/estimates/{estimate_id}/proposal/accept", response_model=None)
def post_accept_tier(estimate_id: UUID, payload: AcceptIn, _: None = Depends(require_module("proposals")), current_user: dict = Depends(_require_dispatch), db: Session = Depends(get_db)) -> Estimate:
    return accept_tier(estimate_id, payload.tier_id, db, actor=resolve_audit_actor(current_user))

@router.get("/proposals/{token}")
def get_public_proposal(token: str, request: Request = None, db: Session = Depends(get_db)) -> dict[str, object]:
    # deleted_at + sent_at are BOTH part of the lookup, not an afterthought.
    # public_token is minted at estimate CREATE, so without these every draft
    # and every soft-deleted estimate is a live public URL. The sibling public
    # route (core/payments.py) already filters deleted_at; sent_at is the
    # additional "we actually sent this to a customer" gate, which is exactly
    # what this endpoint claims to serve — record_customer_view below is even
    # handed sent_at to de-dupe against. A wrong token and an unsent one return
    # the same 404 so the response can't be used to probe which is which.
    est = db.execute(select(Estimate).where(
        Estimate.public_token == token,
        Estimate.deleted_at.is_(None),
        Estimate.sent_at.is_not(None),
    )).scalar_one_or_none()
    if not est: raise HTTPException(status_code=404, detail="Invalid proposal token")  # noqa: E701,E702
    # The customer opened the estimate we sent. Never blocks the response.
    record_customer_view(
        db,
        action="estimate_viewed_by_customer",
        entity_type="estimate",
        entity_id=est.id,
        tenant_id=getattr(est, "company_id", None),
        request=request,
        sent_at=getattr(est, "sent_at", None),
        details={"estimate_number": est.estimate_number},
    )
    tiers = list(db.execute(select(ProposalTier).where(ProposalTier.estimate_id == est.id).order_by(ProposalTier.display_order.asc(), ProposalTier.id.asc())).scalars().all())
    # Explicit projection, NOT the ORM row. This endpoint is public and
    # unauthenticated (see core/customer_views.py), and returning `est` whole
    # handed the caller `notes` — the office's internal notes on the job — plus
    # company_id and a copy of the public_token itself. It went unnoticed
    # because the route was shadowed by the old /api/proposals/{proposal_id}
    # handler and never actually served; retiring that router made it live.
    # Anything a customer should not read stays off this dict by construction:
    # add fields deliberately, never by spreading the model.
    return {
        "estimate": {
            "estimate_number": est.estimate_number,
            "label": est.label,
            "jobsite_address": est.jobsite_address,
            "description": est.description,
            "status": est.status,
            "total": float(est.total) if est.total is not None else 0.0,
            "valid_until": est.valid_until.isoformat() if est.valid_until else None,
            "sent_at": est.sent_at.isoformat() if est.sent_at else None,
            "accepted_at": est.accepted_at.isoformat() if est.accepted_at else None,
            "accepted_tier_id": str(est.accepted_tier_id) if est.accepted_tier_id else None,
            "proposal_mode": bool(est.proposal_mode),
        },
        "tiers": [
            {
                "id": str(t.id),
                "tier_name": t.tier_name,
                "description": t.description,
                "total_price": float(t.total_price) if t.total_price is not None else 0.0,
                "includes_parts": bool(t.includes_parts),
                "warranty_months": t.warranty_months,
                "stripe_payment_link": t.stripe_payment_link,
                "display_order": t.display_order,
            }
            for t in tiers
        ],
    }
