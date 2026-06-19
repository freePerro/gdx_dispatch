from __future__ import annotations

import asyncio
import secrets
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event, utcnow
from gdx_dispatch.modules.proposals.models import Estimate, ProposalTier


def create_estimate(job_id: UUID, estimate_number: str, db: Session, company_id: str = "tenant-test") -> Estimate:
    # company_id is NOT NULL (Build Rule 5). Default is only used by unit
    # tests that call this helper directly; production routers pull the real
    # tenant id from request.state.tenant and pass it explicitly.
    row = Estimate(
        job_id=job_id,
        estimate_number=estimate_number,
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=company_id,
    )
    db.add(row); db.commit(); db.refresh(row); return row  # noqa: E701,E702


def add_proposal_tier(estimate_id: UUID, tier_name: str, description: str | None, total_price: float, warranty_months: int, db: Session) -> ProposalTier:
    est = db.execute(select(Estimate).where(Estimate.id == estimate_id)).scalar_one_or_none()
    if not est: raise HTTPException(status_code=404, detail="Estimate not found")  # noqa: E701,E702
    row = ProposalTier(estimate_id=estimate_id, tier_name=tier_name, description=description, total_price=total_price, warranty_months=warranty_months, display_order={"good": 0, "better": 1, "best": 2}.get(tier_name, 0))
    db.add(row); db.commit(); db.refresh(row); return row  # noqa: E701,E702


def accept_tier(estimate_id: UUID, tier_id: UUID, db: Session) -> Estimate:
    est = db.execute(select(Estimate).where(Estimate.id == estimate_id)).scalar_one_or_none()
    if not est: raise HTTPException(status_code=404, detail="Estimate not found")  # noqa: E701,E702
    tier = db.execute(select(ProposalTier).where(ProposalTier.id == tier_id, ProposalTier.estimate_id == estimate_id)).scalar_one_or_none()
    if not tier: raise HTTPException(status_code=404, detail="Proposal tier not found")  # noqa: E701,E702
    est.status = "accepted"; est.accepted_at = utcnow(); est.accepted_tier_id = tier_id  # noqa: E701,E702
    asyncio.run(log_audit_event(db, "proposal_tier_accepted", "system", "estimate", str(est.id), {"tier_id": str(tier_id), "tier_name": tier.tier_name}))
    db.commit(); db.refresh(est); return est  # noqa: E701,E702
