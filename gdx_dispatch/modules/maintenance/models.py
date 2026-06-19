from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class ServicePlan(TenantBase):
    __tablename__ = "service_plans"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    price_monthly: Mapped[float | None] = mapped_column(Numeric(10, 2))
    price_annual: Mapped[float | None] = mapped_column(Numeric(10, 2))
    visits_per_year: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    includes_parts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stripe_price_id_monthly: Mapped[str | None] = mapped_column(String(100))
    stripe_price_id_annual: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CustomerPlanEnrollment(TenantBase):
    __tablename__ = "customer_plan_enrollments"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    plan_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("service_plans.id"), nullable=False)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(Enum("active", "paused", "canceled", name="customer_plan_status"), nullable=False, default="active")
    next_service_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
