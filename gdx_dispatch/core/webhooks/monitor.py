from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_role
from gdx_dispatch.core.webhooks.models import WebhookDelivery, WebhookEndpoint

router = APIRouter()


@router.get("/api/webhooks/stats", dependencies=[Depends(require_role("owner", "admin"))])
def get_webhook_stats(db: Session = Depends(get_db)):
    stats_query = (
        select(
            WebhookEndpoint.id.label("endpoint_id"),
            WebhookEndpoint.url,
            func.count(WebhookDelivery.id).label("total"),
            func.sum(case((WebhookDelivery.status == "delivered", 1), else_=0)).label("delivered"),
            func.sum(case((WebhookDelivery.status == "failed", 1), else_=0)).label("failed"),
            func.sum(case((WebhookDelivery.status == "abandoned", 1), else_=0)).label("abandoned"),
            func.sum(case((WebhookDelivery.status == "pending", 1), else_=0)).label("pending"),
        )
        .join(WebhookEndpoint)
        .group_by(WebhookEndpoint.id, WebhookEndpoint.url)
    )

    stats = db.execute(stats_query).fetchall()

    return {
        "stats": [
            {
                "endpoint_id": stat.endpoint_id,
                "url": stat.url,
                "total": stat.total,
                "delivered": stat.delivered,
                "failed": stat.failed,
                "abandoned": stat.abandoned,
                "pending": stat.pending,
                "delivery_rate": stat.delivered / stat.total if stat.total > 0 else 0.0,
            }
            for stat in stats
        ]
    }


@router.get("/api/webhooks/deliveries", dependencies=[Depends(require_role("owner", "admin"))])
def get_recent_deliveries(
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    query = select(WebhookDelivery).order_by(WebhookDelivery.created_at.desc()).limit(limit)

    if status:
        query = query.filter(WebhookDelivery.status == status)

    deliveries = db.execute(query).scalars().all()

    return {
        "deliveries": [
            {
                "id": delivery.id,
                "endpoint_id": delivery.endpoint_id,
                "event_type": delivery.event_type,
                "status": delivery.status,
                "attempt_count": delivery.attempt_count,
                "last_attempt_at": delivery.last_attempt_at,
                "next_retry_at": delivery.next_retry_at,
                "response_status": delivery.response_status,
                "idempotency_key": delivery.idempotency_key,
                "created_at": delivery.created_at,
            }
            for delivery in deliveries
        ]
    }


@router.post(
    "/api/webhooks/deliveries/{delivery_id}/retry",
    dependencies=[Depends(require_role("owner", "admin"))],
)
def retry_delivery(delivery_id: UUID, db: Session = Depends(get_db)):
    delivery = db.get(WebhookDelivery, delivery_id)

    if not delivery:
        raise HTTPException(status_code=404, detail="Delivery not found")

    if delivery.status != "abandoned":
        raise HTTPException(status_code=400, detail="Delivery status must be abandoned to retry")

    delivery.status = "pending"
    delivery.attempt_count = 0
    delivery.next_retry_at = None

    db.commit()

    return {"message": "queued for retry", "delivery_id": str(delivery.id)}
