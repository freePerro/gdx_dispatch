from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class InventoryPurchaseOrder(TenantBase):
    """Inventory-workflow PO model used by the purchase_orders module service."""
    __tablename__ = "inventory_purchase_orders"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    po_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    vendor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    vendor_email: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(Enum("draft", "sent", "acknowledged", "received", "closed", "voided", name="inv_po_status"), nullable=False, default="draft")
    total_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    notes: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InventoryPurchaseOrderLine(TenantBase):
    """Inventory-workflow PO line model."""
    __tablename__ = "inventory_purchase_order_lines"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    po_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("inventory_purchase_orders.id"), nullable=False)
    part_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_cost: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    line_total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
