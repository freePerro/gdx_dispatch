from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.control.models import Base, utcnow


class PartAlias(Base):
    __tablename__ = "part_aliases"
    __table_args__ = (
        UniqueConstraint("source_tenant_id", "target_tenant_id", "source_sku", name="uq_part_alias_source_target_sku"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    target_tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    source_sku: Mapped[str] = mapped_column(String(100), nullable=False)
    target_sku: Mapped[str] = mapped_column(String(100), nullable=False)
    source_description: Mapped[str | None] = mapped_column(String(500))
    target_description: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


def resolve_upstream_sku(source_tenant_id: UUID, target_tenant_id: UUID, local_sku: str, db: Session) -> str | None:
    q = select(PartAlias.target_sku).where(
        PartAlias.source_tenant_id == source_tenant_id,
        PartAlias.target_tenant_id == target_tenant_id,
        PartAlias.source_sku == local_sku,
    )
    return db.execute(q).scalar_one_or_none()


def resolve_local_sku(source_tenant_id: UUID, target_tenant_id: UUID, upstream_sku: str, db: Session) -> str | None:
    q = select(PartAlias.source_sku).where(
        PartAlias.source_tenant_id == source_tenant_id,
        PartAlias.target_tenant_id == target_tenant_id,
        PartAlias.target_sku == upstream_sku,
    )
    return db.execute(q).scalar_one_or_none()


def create_alias(source_tenant_id: UUID, target_tenant_id: UUID, source_sku: str, target_sku: str, db: Session) -> PartAlias:
    q = select(PartAlias).where(
        PartAlias.source_tenant_id == source_tenant_id,
        PartAlias.target_tenant_id == target_tenant_id,
        PartAlias.source_sku == source_sku,
    )
    alias = db.execute(q).scalar_one_or_none()
    if alias is None:
        alias = PartAlias(source_tenant_id=source_tenant_id, target_tenant_id=target_tenant_id, source_sku=source_sku, target_sku=target_sku)
        db.add(alias)
    else:
        alias.target_sku = target_sku
    return alias
