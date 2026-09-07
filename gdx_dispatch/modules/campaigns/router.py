from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

# GET/POST /campaigns and POST /campaigns/{id}/send left this module 2026-09-06 (#569):
# routers/campaigns.py registers them first, so FastAPI never dispatched here. Only
# /stats is unique to this module.
router = APIRouter(prefix="/api", tags=["campaigns"], dependencies=[Depends(require_module("campaigns"))])

def _ensure_campaign_tables(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS marketing_campaigns (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                segment_id TEXT NOT NULL,
                template_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                campaign_type TEXT NOT NULL,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
    )
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS marketing_campaign_sends (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                customer_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                status TEXT NOT NULL,
                sent_at TEXT,
                opened_at TEXT,
                clicked_at TEXT,
                converted_at TEXT,
                created_at TEXT
            )
            """
        )
    )


def _campaign_or_404(campaign_id: str, db: Session) -> dict[str, Any]:
    _ensure_campaign_tables(db)
    row = db.execute(
        text(
            """
            SELECT id, name, segment_id, template_id, channel, campaign_type, created_at
            FROM marketing_campaigns
            WHERE id = :campaign_id
            LIMIT 1
            """
        ),
        {"campaign_id": campaign_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return dict(row)


@router.get("/campaigns/{campaign_id}/stats", response_model=None)
async def get_campaign_stats(
    campaign_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _campaign_or_404(campaign_id, db)
    counts = db.execute(
        text(
            """
            SELECT
                COUNT(*) AS sent,
                SUM(CASE WHEN opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opened,
                SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicked,
                SUM(CASE WHEN converted_at IS NOT NULL THEN 1 ELSE 0 END) AS converted
            FROM marketing_campaign_sends
            WHERE campaign_id = :campaign_id
            """
        ),
        {"campaign_id": campaign_id},
    ).mappings().first() or {"sent": 0, "opened": 0, "clicked": 0, "converted": 0}

    return {
        "campaign_id": campaign_id,
        "sent": int(counts.get("sent") or 0),
        "opened": int(counts.get("opened") or 0),
        "clicked": int(counts.get("clicked") or 0),
        "converted": int(counts.get("converted") or 0),
    }
