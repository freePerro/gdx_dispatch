from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class Contractor(TenantBase):
    __tablename__ = "contractors"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    company_name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(30))
    email: Mapped[str | None] = mapped_column(String(200))
    specialty: Mapped[list | None] = mapped_column(JSON)
    license_number: Mapped[str | None] = mapped_column(String(100))
    insurance_expiry: Mapped[date | None] = mapped_column(Date)
    hourly_rate: Mapped[float | None] = mapped_column(Numeric(10, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContractorAssignment(TenantBase):
    __tablename__ = "contractor_assignments"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str | None] = mapped_column(String(100))
    contractor_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("contractors.id"), nullable=False)
    job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    scheduled_date: Mapped[date] = mapped_column(Date, nullable=False)
    hours_worked: Mapped[float | None] = mapped_column(Numeric(10, 2))
    total_cost: Mapped[float | None] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
