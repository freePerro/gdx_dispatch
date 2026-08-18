import asyncio
from datetime import timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import utcnow
from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.core.webhooks.delivery import deliver_webhook
from gdx_dispatch.core.webhooks.models import WebhookDelivery, WebhookEndpoint

# A row committed 'pending' with next_retry_at=NULL is one whose after_commit
# enqueue never happened (broker down at dispatch time) — rescue it once it's
# had a moment to have been dispatched normally, so the sweep isn't racing the
# hook on the happy path.
_STRAND_GRACE_SECONDS = 30

# When the sweep re-enqueues a row it claims it by pushing next_retry_at one beat
# interval forward, so a worker that's slow (or a task still queued at the next
# tick) doesn't get the SAME row re-enqueued every 5 minutes → duplicate POSTs.
# deliver_webhook overwrites next_retry_at on its attempt (None on success); if
# the worker never runs, the row is reclaimed after this window.
_CLAIM_SECONDS = 300


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
    strand_cutoff = now - timedelta(seconds=_STRAND_GRACE_SECONDS)
    with _tenant_session() as db:
        due = db.execute(
            select(WebhookDelivery.id).where(
                WebhookDelivery.status == "pending",
                or_(
                    # normal retry: its backoff window has elapsed
                    and_(
                        WebhookDelivery.next_retry_at.is_not(None),
                        WebhookDelivery.next_retry_at <= now,
                    ),
                    # stranded on dispatch: never enqueued, no backoff set
                    and_(
                        WebhookDelivery.next_retry_at.is_(None),
                        WebhookDelivery.created_at <= strand_cutoff,
                    ),
                ),
            )
        ).scalars().all()
        if due:
            # Claim before enqueue: bump next_retry_at forward so the next tick
            # won't re-enqueue a row a worker is still (or about to be) handling.
            db.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id.in_(due))
                .values(next_retry_at=now + timedelta(seconds=_CLAIM_SECONDS))
            )
            db.commit()
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
