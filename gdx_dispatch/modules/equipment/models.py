from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Date, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class CustomerEquipment(TenantBase):
    __tablename__ = "customer_equipments"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    equipment_type: Mapped[str] = mapped_column(Enum("garage_door", "opener", "gate", "other", name="equipment_type"), nullable=False)
    manufacturer: Mapped[str | None] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(100))
    serial_number: Mapped[str | None] = mapped_column(String(100))
    installation_date: Mapped[date | None] = mapped_column(Date)
    last_service_date: Mapped[date | None] = mapped_column(Date)
    warranty_expires_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EquipmentServiceHistory(TenantBase):
    __tablename__ = "equipment_service_history"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    equipment_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("customer_equipments.id"), nullable=False)
    job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"))
    service_type: Mapped[str] = mapped_column(String(100), nullable=False)
    technician_id: Mapped[str] = mapped_column(String(50), nullable=False)
    service_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    notes: Mapped[str | None] = mapped_column(Text)
    parts_used: Mapped[list[dict] | None] = mapped_column(JSON)
