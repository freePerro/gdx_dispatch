from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow

Base = TenantBase


class CatalogItem(Base):
    """A SKU in the wholesale catalog."""

    __tablename__ = "catalog_items"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    wholesaler_tenant_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    sku: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    base_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class PricingTier(Base):
    """Pricing overrides per distributor for wholesale catalog items."""

    __tablename__ = "pricing_tiers"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    wholesaler_tenant_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    distributor_tenant_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    tier_name: Mapped[str] = mapped_column(String(50), nullable=False)
    discount_pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ChannelAnalytic(Base):
    """Aggregated channel analytics for a wholesaler."""

    __tablename__ = "channel_analytics"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    wholesaler_tenant_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    active_distributors: Mapped[int | None] = mapped_column(default=0)
    total_channel_revenue: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
