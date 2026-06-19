"""SS-27 cross-tenant sharing model stub additions.

NOTE: TODO — these definitions live on a separate declarative
Base (``SS27Base``) and are NOT yet mounted on the primary platform Base
in ``gdx_dispatch/models/platform.py``. The SS-27 Alembic migration
(``TODO_ss27_cross_tenant_sharing_XXXX.py``) is on placeholder
``down_revision = "TODO"``.

Tables:
    - cross_tenant_share             — sharer → sharee grant on a resource
    - cross_tenant_share_acceptance  — record of the sharee's acceptance

Columns favor ``String`` over FK enforcement so the stub stays backend-
portable (sqlite for tests, PG in prod). Real FKs land at integration time.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import declarative_base

SS27Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CrossTenantShare(SS27Base):
    """One sharer-tenant → sharee-tenant grant on a single resource.

    Capabilities is a JSON list of strings (e.g. ``["read","write"]``).
    ``expires_at`` NULL means "no expiry"; ``revoked_at`` NULL means
    "still active".

    Idempotency: ``(sharer_tenant_id, sharee_tenant_id, resource_type,
    resource_id)`` is unique while the share is active — re-calling
    ``create_share`` with the same args returns the existing row
    (see :mod:`gdx_dispatch.core.cross_tenant_sharing`).
    """

    __tablename__ = "cross_tenant_share"
    __table_args__ = (
        Index(
            "ix_cts_sharer",
            "sharer_tenant_id",
        ),
        Index(
            "ix_cts_sharee",
            "sharee_tenant_id",
        ),
        Index(
            "ix_cts_resource",
            "resource_type",
            "resource_id",
        ),
        UniqueConstraint(
            "sharer_tenant_id",
            "sharee_tenant_id",
            "resource_type",
            "resource_id",
            name="uq_cts_active_share",
        ),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    sharer_tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    sharee_tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    resource_type = Column(String(64), nullable=False)
    resource_id = Column(String(128), nullable=False)
    # list[str] — e.g. ["read","write","aggregate"]
    capabilities = Column(JSON, nullable=False)
    # bcrypt hash of the single-use acceptance token (URL-safe 128-bit).
    acceptance_token_hash = Column(String(128), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by_identity_id = Column(String(64), nullable=True)
    created_by_identity_id = Column(String(64), nullable=False)
    shared_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class CrossTenantShareAcceptance(SS27Base):
    """Record of the sharee's acceptance of a share.

    One row per acceptance attempt. Single-use: presenting the same
    acceptance token twice is denied at the helper layer.
    """

    __tablename__ = "cross_tenant_share_acceptance"
    __table_args__ = (
        Index("ix_ctsa_share", "share_id"),
        UniqueConstraint("share_id", name="uq_ctsa_one_accept"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    share_id = Column(Uuid(as_uuid=True), nullable=False)
    accepted_by_identity_id = Column(String(64), nullable=False)
    accepted_by_tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
