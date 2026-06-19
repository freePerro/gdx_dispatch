from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow

Base = TenantBase


class DealerOrder(Base):
    """An order from a dealer to their distributor."""

    __tablename__ = "dealer_orders"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    dealer_tenant_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    distributor_tenant_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    order_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    # pending → confirmed → shipped → delivered → cancelled
    line_items: Mapped[dict | None] = mapped_column(JSON)
    total_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(100), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DistributorAnalytic(Base):
    """Aggregated analytics snapshot for a distributor's dealer network."""

    __tablename__ = "distributor_analytics"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    distributor_tenant_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    active_dealers: Mapped[int | None] = mapped_column(default=0)
    total_orders: Mapped[int | None] = mapped_column(default=0)
    total_revenue: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
