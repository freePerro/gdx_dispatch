from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow

# Canonical model lives in the router — single source of truth
from gdx_dispatch.routers.gps import TechnicianLocation  # noqa: F401

Base = TenantBase


class DispatchRoute(Base):
    __tablename__ = "dispatch_routes"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    technician_id: Mapped[str] = mapped_column(String(50), nullable=False)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("jobs.id"), nullable=False)
    estimated_arrival: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    distance_km: Mapped[float | None] = mapped_column(Numeric(8, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
