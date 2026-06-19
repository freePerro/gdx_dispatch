"""SS-28 consumer-audit model stub additions.

NOTE: TODO — these definitions live on a separate
declarative Base (``SS28Base``) and are NOT yet mounted on the primary
platform Base in ``gdx_dispatch/models/platform.py``. The SS-28 Alembic
migration (``TODO_ss28_audit_XXXX.py``) is on placeholder
``down_revision = "TODO"``.

Tables:
    - platform_consumer_audit   — append-only per-request audit rows
    - audit_retention_policy     — per-tenant retention window (days)
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import declarative_base

SS28Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PlatformConsumerAudit(SS28Base):
    """One row per platform-consumer API/tool invocation.

    Append-only. Mutations are a tamper signal — :mod:`audit_hash_chain`
    will detect them. Soft-delete is NOT supported; retention pruning
    is the only legitimate delete path (see ``audit_retention_cron``).
    """

    __tablename__ = "platform_consumer_audit"
    __table_args__ = (
        Index("ix_sca_tenant_created", "tenant_id", "created_at"),
        Index("ix_sca_principal", "principal_identity_id"),
        Index("ix_sca_action", "action"),
        Index("ix_sca_resource", "resource_type", "resource_id"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    principal_identity_id = Column(String(64), nullable=True)
    action = Column(String(128), nullable=False)
    resource_type = Column(String(64), nullable=False)
    resource_id = Column(String(128), nullable=True)
    result = Column(String(32), nullable=False)  # "ok" | "denied" | "error"
    details = Column(JSON, nullable=True)
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    # Chain columns — both sha256 hex, 64 chars.
    prev_hash = Column(String(64), nullable=False)
    row_hash = Column(String(64), nullable=False)


class AuditRetentionPolicy(SS28Base):
    """Per-tenant retention window for ``platform_consumer_audit``.

    ``retention_days`` defaults to 90 if a tenant has no explicit row.
    ``audit_retention_cron`` reads this table and prunes older rows,
    but never prunes within the current calendar month (safety floor).
    """

    __tablename__ = "audit_retention_policy"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_arp_tenant"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    retention_days = Column(Integer, nullable=False, default=90)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
