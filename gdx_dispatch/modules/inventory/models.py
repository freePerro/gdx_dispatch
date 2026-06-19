from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class Part(TenantBase):
    __tablename__ = "parts"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    sku: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    unit_cost: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    qty_on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reorder_point: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    vendor_name: Mapped[str | None] = mapped_column(String(200))
    vendor_sku: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobPart(TenantBase):
    __tablename__ = "job_parts"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=False)
    part_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("parts.id"), nullable=False)
    qty_used: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_cost_at_time: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
