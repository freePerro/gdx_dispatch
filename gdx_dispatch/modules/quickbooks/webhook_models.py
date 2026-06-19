from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class QBWebhookEvent(TenantBase):
    """Deduplication table for incoming QuickBooks change-notification webhooks.

    QB sends a notification for each entity change (Customer, Invoice, etc.).
    We store each unique (realm_id, entity_name, entity_id, operation) tuple so
    that retried or duplicated deliveries are silently skipped.
    """

    __tablename__ = "qb_webhook_events"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event_id: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(100), nullable=False)
    realm_id: Mapped[str] = mapped_column(String(50), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
