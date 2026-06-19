from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class Campaign(TenantBase):
    __tablename__ = "campaigns"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    trigger: Mapped[str] = mapped_column(Enum("estimate_not_accepted", "job_completed", "manual", name="campaign_trigger"), nullable=False, default="estimate_not_accepted")
    delay_days: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    message_template: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Enum("sms", "email", "both", name="campaign_channel"), nullable=False, default="sms")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    send_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CampaignSend(TenantBase):
    __tablename__ = "campaign_sends"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    campaign_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("campaigns.id"), nullable=False)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(50), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Enum("pending", "sent", "failed", "cancelled", name="campaign_send_status"), nullable=False, default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
