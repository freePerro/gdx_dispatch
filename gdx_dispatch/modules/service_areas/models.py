from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class ServiceArea(TenantBase):
    __tablename__ = "service_areas"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    zip_codes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    radius_miles: Mapped[float | None] = mapped_column(Float)
    center_lat: Mapped[float | None] = mapped_column(Float)
    center_lng: Mapped[float | None] = mapped_column(Float)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ServiceAreaTechnician(TenantBase):
    __tablename__ = "service_area_technicians"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    service_area_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("service_areas.id"), nullable=False)
    technician_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
