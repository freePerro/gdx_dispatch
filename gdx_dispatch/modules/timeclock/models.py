from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class TimeClock(TenantBase):
    __tablename__ = "timeclocks"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    technician_id: Mapped[str] = mapped_column(String(50), nullable=False)
    job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"))
    clock_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    clock_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    labor_minutes: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    clock_in: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    clock_out: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=True)
    gps_accuracy: Mapped[float] = mapped_column(Float, nullable=True)
    lat: Mapped[float] = mapped_column(Float, nullable=True)
    lng: Mapped[float] = mapped_column(Float, nullable=True)
    signature_data: Mapped[str] = mapped_column(Text, nullable=True)
    signed_by: Mapped[str] = mapped_column(String(255), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(50), nullable=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=True)
