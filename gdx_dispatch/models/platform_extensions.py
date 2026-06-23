"""SQLAlchemy models for SS-3 platform extensions.

Mirrors migrations 004a-004d. 16 tables across 4 logical groups:

  3a (OAuth surface):     OAuthClient, OAuthClientKey, BillingAccount,
                          Installation, AccessToken, RevocationDenylistEntry
  3b (events + meter +    EventOutbox, MeterEvent, AuditLog
       audit):
  3c (sharing):           SharedResource, Share, ResourceDescriptor,
                          ResourceFieldDescriptor
  3d (supporting):        DeveloperAccount, SandboxEnv, SsoConfig

(BillingAccount is here even though SS-3a creates the stub — the canonical
ORM definition lives here; the stub matches the schema. DeveloperAccount lives
under 3d but is included in this file to keep all SS-3 models in one place.)

Patterns mirror gdx_dispatch/models/platform.py (SS-2). Portable types only:
- Uuid(as_uuid=True) — works on PG + SQLite via SA's dialect translation.
- JSON — translated to JSONB on PG, TEXT on SQLite.
- ForeignKey strings reference table names; use_alter=True only where there's
  a known circular dependency that breaks SA metadata sort.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from gdx_dispatch.control.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── 3a: OAuth surface ───────────────────────────────────────────────────────


class OAuthClient(Base):
    __tablename__ = "oauth_clients"
    __table_args__ = (
        Index("ix_oauth_clients_owner", "owner_type", "owner_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    owner_type: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    redirect_uris: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    scopes_requested: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    client_type: Mapped[str] = mapped_column(String(32), nullable=False)
    homepage_url: Mapped[str | None] = mapped_column(String(512))
    logo_url: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    keys: Mapped[list[OAuthClientKey]] = relationship(back_populates="oauth_client", cascade="all, delete-orphan")
    installations: Mapped[list[Installation]] = relationship(back_populates="oauth_client")


class OAuthClientKey(Base):
    __tablename__ = "oauth_client_keys"
    __table_args__ = (
        UniqueConstraint("oauth_client_id", "kid", name="uq_client_keys_kid"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    oauth_client_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("oauth_clients.id", ondelete="CASCADE"), nullable=False
    )
    kid: Mapped[str] = mapped_column(String(64), nullable=False)
    public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    oauth_client: Mapped[OAuthClient] = relationship(back_populates="keys")


class BillingAccount(Base):
    __tablename__ = "billing_accounts"
    __table_args__ = (
        Index("ix_billing_owner", "owner_type", "owner_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_type: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    payment_method_id: Mapped[str | None] = mapped_column(String(64))
    invoice_email: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Installation(Base):
    __tablename__ = "installations"
    __table_args__ = (
        UniqueConstraint("oauth_client_id", "tenant_id", name="uq_install_app_tenant"),
        Index("ix_installations_tenant", "tenant_id"),
        Index("ix_installations_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    oauth_client_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("oauth_clients.id"), nullable=False
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # ponytail: identities/capability_sets tables were removed with the
    # multi-tenant SaaS surface; these stay plain UUID columns (no FK) since
    # there's no table to reference. Re-add the FK if those tables ever return.
    installer_identity_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    capability_set_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    billing_account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("billing_accounts.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    uninstalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    health_status: Mapped[str] = mapped_column(String(32), nullable=False, default="healthy")
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    oauth_client: Mapped[OAuthClient] = relationship(back_populates="installations")
    access_tokens: Mapped[list[AccessToken]] = relationship(back_populates="installation")


class AccessToken(Base):
    __tablename__ = "access_tokens"
    __table_args__ = (
        Index("ix_access_tokens_installation", "installation_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_type: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    installation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("installations.id")
    )
    # ponytail: capability_sets table removed with multi-tenant surface — plain UUID, no FK.
    capability_set_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    name: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    installation: Mapped[Installation | None] = relationship(back_populates="access_tokens")

    # ── SS-15 merge (0.9-a) ────────────────────────────────────────────────
    # Admin-issued PAT lifecycle state. Column default keeps existing rows
    # on 'active'; the admin-pats router transitions this through
    # 'pending_approval' → 'active' | 'revoked'. ``revoked_at`` remains the
    # authoritative revocation timestamp; 'revoked' here is derivable.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    # Free-form audit-supporting metadata. For admin-issued tokens:
    # {"issued_by_admin_identity_id": "<uuid>", "target_identity_id": "<uuid>",
    #  "approved_by": "<uuid>", "approved_at": "<iso-ts>"}.
    # Named metadata_json (not metadata) because SQLAlchemy's declarative
    # base reserves 'metadata'.
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class RevocationDenylistEntry(Base):
    __tablename__ = "revocation_denylist"
    __table_args__ = (
        Index("ix_denylist_jti", "token_jti"),
        Index("ix_denylist_identity", "identity_id"),
        Index("ix_denylist_install", "installation_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    token_jti: Mapped[str | None] = mapped_column(String(64))
    # ponytail: identities table removed with multi-tenant surface — plain UUID, no FK.
    identity_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    installation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("installations.id")
    )
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── 3b: events + meter + audit ──────────────────────────────────────────────


class EventOutbox(Base):
    __tablename__ = "event_outbox"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    tenant_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"))
    installation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("installations.id")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    emitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MeterEvent(Base):
    __tablename__ = "meter_events"
    __table_args__ = (
        Index("ix_meter_events_install_time", "installation_id", "occurred_at"),
        Index("ix_meter_events_billing_time", "billing_account_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    installation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("installations.id"), nullable=False
    )
    billing_account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("billing_accounts.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    dimensions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class AuditLog(Base):
    """Platform-aware audit log row.

    In control DB this table is created fresh by SS-3b. In per-tenant DBs the
    table predates SS-3 (per-tenant audit chain); SS-3b ALTER ADDs the
    platform columns. This model definition matches the post-SS-3b control DB
    shape; per-tenant tables may have additional columns this model doesn't see.
    """
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_install", "installation_id"),
        Index("ix_audit_shared_via", "shared_via_resource_id"),
        Index("ix_audit_tenant_time", "tenant_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"))
    user_id: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    hash: Mapped[str | None] = mapped_column(String(64))
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    # SS-3 platform columns:
    installation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("installations.id")
    )
    agent_identity: Mapped[str | None] = mapped_column(String(255))
    shared_via_resource_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        # FK wired in SS-3c (shared_resources doesn't exist in 3b's revision).
        ForeignKey("shared_resources.id", use_alter=True, name="fk_audit_shared_via_resource"),
    )
    act_chain: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)


# ── 3c: sharing ─────────────────────────────────────────────────────────────


class ResourceDescriptor(Base):
    __tablename__ = "resource_descriptors"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    resource_type: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    schema: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    capabilities_supported: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    introspection_endpoint: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    field_descriptors: Mapped[list[ResourceFieldDescriptor]] = relationship(
        back_populates="resource_descriptor", cascade="all, delete-orphan"
    )


class ResourceFieldDescriptor(Base):
    __tablename__ = "resource_field_descriptors"
    __table_args__ = (
        UniqueConstraint("resource_descriptor_id", "tenant_id", "field_name", name="uq_field_descriptor"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    resource_descriptor_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resource_descriptors.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"))
    field_name: Mapped[str] = mapped_column(String(128), nullable=False)
    field_type: Mapped[str] = mapped_column(String(32), nullable=False)
    sensitivity_classification: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    resource_descriptor: Mapped[ResourceDescriptor] = relationship(back_populates="field_descriptors")


class SharedResource(Base):
    __tablename__ = "shared_resources"
    __table_args__ = (
        Index("ix_shared_resources_owner", "owner_tenant_id"),
        Index("ix_shared_resources_resource", "resource_type", "resource_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # TEXT not UUID: GDX has mixed PK types. No FK by design (v2 patch O3).
    resource_id: Mapped[str] = mapped_column(Text, nullable=False)
    shared_via_installation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("installations.id")
    )
    visibility: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    shares: Mapped[list[Share]] = relationship(back_populates="shared_resource", cascade="all, delete-orphan")


class Share(Base):
    __tablename__ = "shares"
    __table_args__ = (
        Index("ix_shares_target_tenant", "target_tenant_id"),
        Index("ix_shares_target_install", "target_installation_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    shared_resource_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shared_resources.id", ondelete="CASCADE"), nullable=False
    )
    target_tenant_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"))
    target_installation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("installations.id")
    )
    capabilities: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    shared_resource: Mapped[SharedResource] = relationship(back_populates="shares")


# ── 3d: supporting ──────────────────────────────────────────────────────────


class DeveloperAccount(Base):
    __tablename__ = "developer_accounts"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class SandboxEnv(Base):
    __tablename__ = "sandbox_envs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    subdomain: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="provisioning")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    last_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    torn_down_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SsoConfig(Base):
    __tablename__ = "sso_configs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    federation_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="hybrid")
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    authoritative_domains: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
