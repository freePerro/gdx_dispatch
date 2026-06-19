from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class WorkflowRule(TenantBase):
    __tablename__ = "workflow_rules"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    trigger_event: Mapped[str] = mapped_column(String(100), nullable=False)
    conditions: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    actions: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class WorkflowRun(TenantBase):
    __tablename__ = "workflow_runs"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    rule_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("workflow_rules.id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(50), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    status: Mapped[str] = mapped_column(Enum("success", "failed", "skipped", name="workflow_run_status"), nullable=False, default="success")
    actions_run: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(Text)
