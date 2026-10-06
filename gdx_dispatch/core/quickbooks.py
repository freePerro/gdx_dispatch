"""QuickBooks tables and the "is it in QB" signal.

The models, the two error classes and ``qb_entity_is_mapped`` are all that
live here. The QuickBooks client, sync, pushes and pulls are in
``gdx_dispatch.modules.quickbooks``. The python-quickbooks SDK layer that
used to sit below was never installable in the image and was deleted
(GDXA-260).
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase


class QBError(Exception):
    pass


class QBAuthError(QBError):
    pass


class QBConnection(TenantBase):
    __tablename__ = "qb_connections"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    realm_id: Mapped[str] = mapped_column(String(100), nullable=False)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refresh_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_sync_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=True)
    # Per-tenant override for the Slice 5 delete-detection flag. NULL means
    # "no preference — fall back to the global QB_DELETE_SYNC_ENABLED env var."
    # An admin flips this from the Reconciliation tab to pilot delete sync on
    # one tenant without touching the global env var. The env var remains a
    # global kill-switch — when set to 0 it overrides any True column value.
    delete_sync_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class QBEntityMap(TenantBase):
    __tablename__ = "qb_entity_maps"
    __table_args__ = (
        UniqueConstraint("tenant_id", "entity_type", "local_id", name="uq_qb_map_local"),
        UniqueConstraint("tenant_id", "entity_type", "qb_id", name="uq_qb_map_remote"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    local_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    qb_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class QBVendor(TenantBase):
    __tablename__ = "qb_vendors"
    __table_args__ = (UniqueConstraint("tenant_id", "qb_vendor_id", name="uq_qb_vendor_tenant"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    qb_vendor_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=True)
    phone: Mapped[str] = mapped_column(String(50), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


def qb_entity_is_mapped(db: Session, entity_type: str, local_id: object) -> bool:
    """Authoritative "is this record in QuickBooks" signal.

    A ``QBEntityMap`` row links a local record (``entity_type`` + ``local_id``)
    to its QuickBooks id. Every sync path writes it via
    ``modules/quickbooks/sync._upsert_map`` and the
    QB dashboard's entity counts are exactly these rows — so its presence is the
    single source of truth for "in QB".

    This is deliberately NOT ``Invoice/Customer.qb_synced_at``: that column is
    stamped only by the newer selective-push path and has no backfill, so a
    record synced by the legacy path, imported, or entered manually in QB has
    ``qb_synced_at = NULL`` yet is very much in QuickBooks. Reading NULL as
    "not synced" would contradict the dashboard's own counts.

    Single-tenant-per-DB: the table holds only this tenant's rows, so no
    tenant_id filter is needed. Returns False (never raises) if the table does
    not exist yet on a tenant that has never connected QuickBooks.
    """
    try:
        return db.execute(
            select(QBEntityMap.id)
            .where(
                QBEntityMap.entity_type == entity_type,
                QBEntityMap.local_id == str(local_id),
            )
            .limit(1)
        ).first() is not None
    except Exception:
        db.rollback()
        return False
