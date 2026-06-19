from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class Vehicle(TenantBase):
    __tablename__ = "vehicles"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    vin: Mapped[str | None] = mapped_column(String(17), unique=True)
    make: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    license_plate: Mapped[str | None] = mapped_column(String(20))
    assigned_technician_id: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(Enum("available", "in_use", "maintenance", "retired", name="vehicle_status"), nullable=False, default="available")
    odometer: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_service_odometer: Mapped[int | None] = mapped_column(Integer)
    last_service_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    service_interval_miles: Mapped[int] = mapped_column(Integer, nullable=False, default=3000)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VehicleServiceRecord(TenantBase):
    __tablename__ = "vehicle_service_records"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    vehicle_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("vehicles.id"), nullable=False)
    service_type: Mapped[str] = mapped_column(Enum("oil_change", "tire_rotation", "inspection", "brake_service", "repair", "other", name="vehicle_service_type"), nullable=False)
    mileage_at_service: Mapped[int] = mapped_column(Integer, nullable=False)
    service_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cost: Mapped[float | None] = mapped_column(Numeric(10, 2))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
