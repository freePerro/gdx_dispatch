from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.proposals.models import Estimate, ProposalTier
from gdx_dispatch.modules.proposals.service import accept_tier, add_proposal_tier

router = APIRouter(prefix="/api", tags=["proposals"])

class TierIn(BaseModel): tier_name: str; description: str | None = None; total_price: float = 0; warranty_months: int = 0  # noqa: E701,E702
class AcceptIn(BaseModel): tier_id: UUID  # noqa: E701,E702

@router.get("/estimates/{estimate_id}/proposal", response_model=None)
def get_proposal(estimate_id: UUID, _: None = Depends(require_module("proposals")), db: Session = Depends(get_db)) -> list[ProposalTier]:
    if not db.execute(select(Estimate.id).where(Estimate.id == estimate_id)).scalar_one_or_none(): raise HTTPException(status_code=404, detail="Estimate not found")  # noqa: E701,E702
    return list(db.execute(select(ProposalTier).where(ProposalTier.estimate_id == estimate_id).order_by(ProposalTier.display_order.asc(), ProposalTier.id.asc())).scalars().all())

@router.post("/estimates/{estimate_id}/proposal-tiers", response_model=None)
def post_proposal_tier(estimate_id: UUID, payload: TierIn, _: None = Depends(require_module("proposals")), db: Session = Depends(get_db)) -> ProposalTier:
    return add_proposal_tier(estimate_id, payload.tier_name, payload.description, payload.total_price, payload.warranty_months, db)

@router.post("/estimates/{estimate_id}/proposal/accept", response_model=None)
def post_accept_tier(estimate_id: UUID, payload: AcceptIn, _: None = Depends(require_module("proposals")), db: Session = Depends(get_db)) -> Estimate:
    return accept_tier(estimate_id, payload.tier_id, db)

@router.get("/proposals/{token}")
def get_public_proposal(token: str, db: Session = Depends(get_db)) -> dict[str, object]:
    est = db.execute(select(Estimate).where(Estimate.public_token == token)).scalar_one_or_none()
    if not est: raise HTTPException(status_code=404, detail="Invalid proposal token")  # noqa: E701,E702
    tiers = list(db.execute(select(ProposalTier).where(ProposalTier.estimate_id == est.id).order_by(ProposalTier.display_order.asc(), ProposalTier.id.asc())).scalars().all())
    return {"estimate": est, "tiers": tiers}
