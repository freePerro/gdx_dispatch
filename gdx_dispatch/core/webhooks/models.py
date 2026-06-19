from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow
from gdx_dispatch.core.pii import EncryptedString


class AIAction(TenantBase):
    __tablename__ = "ai_actions"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="normal")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class WebhookEndpoint(TenantBase):
    __tablename__ = "webhook_endpoints"
    # S122-9 slice 2 (2026-05-12): `secret` is back on the EncryptedString
    # TypeDecorator after the two raw-SQL writers (public_router.py:493,
    # public_api.py:395) were refactored to ORM. The lint gate
    # `gdx_dispatch/tools/raw_sql_on_encrypted_columns_scan.py` enforces the contract
    # going forward. Activation depends on MASTER_ENCRYPTION_KEY being set
    # (S122-9 slice 1 already shipped that on prod).
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    secret: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    events: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class WebhookDelivery(TenantBase):
    __tablename__ = "webhook_deliveries"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    endpoint_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("webhook_endpoints.id"), nullable=True)
    event_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        Enum("pending", "delivered", "failed", "abandoned", name="webhook_delivery_status"),
        nullable=False,
        default="pending",
    )
    response_status: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    # -- columns from production schema not yet in ORM --
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    event: Mapped[str | None] = mapped_column(String(100), nullable=True)
    request_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    subscription_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
