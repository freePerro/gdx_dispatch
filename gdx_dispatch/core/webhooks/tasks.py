import asyncio

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import utcnow
from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.core.webhooks.delivery import deliver_webhook
from gdx_dispatch.core.webhooks.models import WebhookDelivery, WebhookEndpoint


def _tenant_session() -> Session:
    """Open a session on the single application database."""
    return SessionLocal()


# No queue= kwarg: a decorator queue overrides task_routes entirely, and
# "high"/"low" are pre-rename queue names no worker consumes (2026-07-07
# audit). task_routes sends webhooks.* to priority:high.
@celery_app.task
def deliver_webhook_task(delivery_id: str) -> None:
    with _tenant_session() as db:
        if db.get(WebhookDelivery, delivery_id):  # noqa: E701,E702
            asyncio.run(deliver_webhook(delivery_id, db))


@celery_app.task
def retry_failed_webhooks_task() -> int:
    now, total = utcnow(), 0
    with _tenant_session() as db:
        due = db.execute(
            select(WebhookDelivery.id).where(
                WebhookDelivery.status == "pending",
                WebhookDelivery.next_retry_at.is_not(None),
                WebhookDelivery.next_retry_at <= now,
            )
        ).scalars().all()
    for did in due:
        deliver_webhook_task.delay(str(did))
        total += 1
    return total


def emit_webhook(event_type: str, entity_id: str, payload: dict, tenant_id: str, db: Session) -> int:
    total = 0
    for ep in db.execute(select(WebhookEndpoint).where(WebhookEndpoint.is_active.is_(True))).scalars().all():
        if event_type not in (ep.events or []):  # noqa: E701,E702
            continue
        row = WebhookDelivery(
            endpoint_id=ep.id,
            event_type=event_type,
            payload=payload,
            idempotency_key=f"{tenant_id}:{event_type}:{entity_id}"[:100],
        )
        db.add(row)
        db.flush()
        deliver_webhook_task.delay(str(row.id))
        total += 1
    db.commit()
    return total
