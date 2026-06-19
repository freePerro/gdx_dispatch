from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow

Base = TenantBase


class TenantHealthScore(Base):
    __tablename__ = "tenant_health_scores"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)  # 0-100
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    factors: Mapped[dict | None] = mapped_column(JSON)  # {jobs_30d, invoices_30d, login_days_30d}
    playbook_triggered: Mapped[str | None] = mapped_column(String(100))
