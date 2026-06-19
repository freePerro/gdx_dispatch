from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class ModuleChangeOrder(TenantBase):
    """Module-internal change order model with signature token workflow."""
    __tablename__ = "module_change_orders"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    co_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Enum("draft", "pending_approval", "approved", "rejected", "void", name="module_co_status"), nullable=False, default="draft")
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(50))
    customer_signature_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ModuleChangeOrderLine(TenantBase):
    """Module-internal change order line."""
    __tablename__ = "module_change_order_lines"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    co_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    line_total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
