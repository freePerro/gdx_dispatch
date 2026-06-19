from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import utcnow
from gdx_dispatch.modules.campaigns.models import Campaign, CampaignSend
from gdx_dispatch.modules.campaigns.tasks import send_campaign_task


def queue_follow_up(campaign_id: UUID, customer_id: UUID, entity_type: str, entity_id: str, db: Session) -> CampaignSend:
    key = f"{campaign_id}:{entity_type}:{entity_id}"[:100]
    exists = db.execute(select(CampaignSend).where(CampaignSend.idempotency_key == key)).scalar_one_or_none()
    if exists:
        return exists
    campaign = db.execute(select(Campaign).where(Campaign.id == campaign_id)).scalar_one_or_none()
    if not campaign:
        raise ValueError("Campaign not found")
    scheduled_at = utcnow() + timedelta(days=campaign.delay_days)
    row = CampaignSend(
        campaign_id=campaign_id,
        customer_id=customer_id,
        entity_type=entity_type,
        entity_id=entity_id,
        scheduled_at=scheduled_at,
        idempotency_key=key,
    )
    db.add(row)
    db.flush()
    send_campaign_task.apply_async(args=[str(row.id)], eta=scheduled_at)
    db.commit()
    db.refresh(row)
    return row


def render_template(template: str, context: dict) -> str:
    text = template
    for k in ("customer_name", "estimate_total", "job_title", "company_name"):
        text = text.replace(f"{{{{{k}}}}}", str(context.get(k, "")))
    return text
