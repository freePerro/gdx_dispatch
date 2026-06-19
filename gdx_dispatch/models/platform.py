from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
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


class Identity(Base):
    __tablename__ = "identities"
    __table_args__ = (
        Index("ix_identities_email", "email"),
        Index("ix_identities_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    providers: Mapped[list[IdentityProvider]] = relationship(
        back_populates="identity", cascade="all, delete-orphan"
    )
    memberships: Mapped[list[Membership]] = relationship(
        back_populates="identity",
        cascade="all, delete-orphan",
        foreign_keys="Membership.identity_id",
    )


class IdentityProvider(Base):
    __tablename__ = "identity_providers"
    __table_args__ = (
        UniqueConstraint("provider_type", "provider_subject", name="uq_idp_provider_subject"),
        Index("ix_idp_identity_id", "identity_id"),
        Index("ix_idp_provider_email", "provider_email"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    identity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("identities.id", ondelete="CASCADE"), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_email: Mapped[str | None] = mapped_column(String(255))
    email_verified_by_provider: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_authoritative_for_domain: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Attribute name avoids clobbering SQLAlchemy's model-level `.metadata`.
    provider_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)

    identity: Mapped[Identity] = relationship(back_populates="providers")


class CapabilitySet(Base):
    __tablename__ = "capability_sets"
    __table_args__ = (
        UniqueConstraint("name", "scope_type", name="uq_capset_name_scope"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    capabilities: Mapped[list[Capability]] = relationship(
        back_populates="capability_set", cascade="all, delete-orphan"
    )


class Capability(Base):
    __tablename__ = "capabilities"
    __table_args__ = (
        Index("ix_capabilities_capset", "capability_set_id"),
        Index("ix_capabilities_resource_type", "resource_type"),
        Index("ix_capabilities_parent", "parent_capability_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    capability_set_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("capability_sets.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    instance_pattern: Mapped[str] = mapped_column(String(255), nullable=False, default="*")
    conditions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    parent_capability_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("capabilities.id", ondelete="CASCADE")
    )
    # FK to installations.id added in SS-3a migration (use_alter=True breaks the
    # circular dep: Capability ← Installation ← CapabilitySet ← Capability).
    granted_via_installation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("installations.id", use_alter=True, name="fk_capabilities_granted_via_install", ondelete="CASCADE"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    capability_set: Mapped[CapabilitySet] = relationship(back_populates="capabilities")
    parent_capability: Mapped[Capability | None] = relationship(
        "Capability",
        remote_side="Capability.id",
        backref="child_capabilities",
    )


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        Index("ix_memberships_identity", "identity_id"),
        Index("ix_memberships_tenant", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    identity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("identities.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    capability_set_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("capability_sets.id"), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    granted_by_identity_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("identities.id"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    identity: Mapped[Identity] = relationship(back_populates="memberships", foreign_keys=[identity_id])
    granted_by_identity: Mapped[Identity | None] = relationship(foreign_keys=[granted_by_identity_id])
    capability_set: Mapped[CapabilitySet] = relationship()


class PendingInvalidation(Base):
    __tablename__ = "pending_invalidations"
    __table_args__ = (
        Index("ix_pending_invalidations_unreplayed", "enqueued_at"),
    )

    identity_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("identities.id", ondelete="CASCADE"), primary_key=True
    )
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    replayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ───────────────────────────────────────────────────────────────────────────
# Sprint 0.9-a — merged model additions from SS-18..SS-35 stubs
# ───────────────────────────────────────────────────────────────────────────
# Each SS's stub was previously a separate declarative_base() island so it
# could ship without mutating the canonical platform metadata graph. 0.9-a
# collapses all of them onto the single canonical Base. The SS<N>_additions
# modules are kept as thin compatibility re-export shims; see
# ai-queue/orchestrator_qa/outbox.md Q-2026-04-19T… for why full stub deletion
# was deferred.


# ── SS-18: MCP tool registration + execution audit ──────────────────────────


class McpToolRegistration(Base):
    __tablename__ = "mcp_tool_registration"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    sensitivity_class: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")
    capabilities_required: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    descriptor: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class McpToolExecutionAudit(Base):
    __tablename__ = "mcp_tool_execution_audit"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    identity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    capabilities_snapshot: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    approval_ref: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)


# ── SS-19: MCP execution log ────────────────────────────────────────────────


class McpExecutionLog(Base):
    __tablename__ = "mcp_execution_log"
    __table_args__ = (
        Index("ix_mcp_execution_log_trace", "trace_id"),
        Index("ix_mcp_execution_log_tool", "tool_name"),
        Index("ix_mcp_execution_log_tenant", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    identity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    capabilities_snapshot: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ── SS-20: developer portal (accounts / email verifications / apps / secrets) ─
# Merged onto canonical Base in slice 0.9-a.1. Tables renamed to the
# `developer_portal_*` prefix to avoid colliding with SS-3d's existing
# `developer_accounts` table (different PK type + columns — see
# ai-queue/orchestrator_qa/outbox.md Q-2026-04-19T01:30:00Z).
#
# Class names here are deliberately `DevPortal*` to avoid clashing with
# SS-3d's `DeveloperAccount`. The SS-20 stub at
# gdx_dispatch/models/platform_ss20_additions.py still exists with its original
# class names (DeveloperAccount, EmailVerification, DeveloperApp,
# DeveloperAppSecret) on `DevPortalBase`; dual-registry is intentional
# until slice 0.9-b migrates consumers to the canonical classes.


class DevPortalAccount(Base):
    __tablename__ = "developer_portal_accounts"
    __table_args__ = (Index("ix_developer_portal_accounts_email", "email", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    tier: Mapped[str] = mapped_column(String(32), nullable=False, default="sandbox")
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tos_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    apps: Mapped[list[DevPortalApp]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    verifications: Mapped[list[DevPortalEmailVerification]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class DevPortalEmailVerification(Base):
    __tablename__ = "developer_portal_email_verifications"
    __table_args__ = (
        Index("ix_developer_portal_email_verif_token", "token", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("developer_portal_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    token: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    account: Mapped[DevPortalAccount] = relationship(back_populates="verifications")


class DevPortalApp(Base):
    __tablename__ = "developer_portal_apps"
    __table_args__ = (Index("ix_developer_portal_apps_client_id", "client_id", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("developer_portal_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[DevPortalAccount] = relationship(back_populates="apps")
    secrets: Mapped[list[DevPortalAppSecret]] = relationship(
        back_populates="app", cascade="all, delete-orphan"
    )


class DevPortalAppSecret(Base):
    __tablename__ = "developer_portal_app_secrets"
    __table_args__ = (
        UniqueConstraint("app_id", "secret_prefix", name="uq_dev_portal_app_secret_prefix"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("developer_portal_apps.id", ondelete="CASCADE"), nullable=False
    )
    secret_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    app: Mapped[DevPortalApp] = relationship(back_populates="secrets")


# ── SS-21: third-party OAuth2 (authz codes + tokens + admin consent + webhooks)
# SS-21 uses its own `ss21_*` prefixed tables. Classes retain SS21-prefixed
# names by convention; __tablename__ unchanged.


class AuthorizationCode(Base):
    __tablename__ = "ss21_authorization_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(128), nullable=False, unique=True, index=True)
    client_id = Column(String(64), nullable=False, index=True)
    redirect_uri = Column(String(1024), nullable=False)
    scope = Column(Text, nullable=False, default="")
    tenant_id = Column(Uuid(as_uuid=True), nullable=True, index=True)
    subject_id = Column(String(64), nullable=True, index=True)
    code_challenge = Column(String(255), nullable=True)
    code_challenge_method = Column(String(16), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    admin_consent = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class OAuthToken(Base):
    __tablename__ = "ss21_oauth_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    access_token = Column(String(128), nullable=False, unique=True, index=True)
    refresh_token = Column(String(128), nullable=False, unique=True, index=True)
    client_id = Column(String(64), nullable=False, index=True)
    scope = Column(Text, nullable=False, default="")
    tenant_id = Column(Uuid(as_uuid=True), nullable=True, index=True)
    subject_id = Column(String(64), nullable=True, index=True)
    issued_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class AdminConsentGrant(Base):
    __tablename__ = "ss21_admin_consent_grants"
    __table_args__ = (
        UniqueConstraint("tenant_id", "client_id", name="uq_ss21_admin_grant_pair"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False, index=True)
    client_id = Column(String(64), nullable=False, index=True)
    scope = Column(Text, nullable=False, default="")
    granted_by = Column(String(64), nullable=False)
    granted_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    status = Column(String(16), nullable=False, default="active")
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by = Column(String(64), nullable=True)


class SS21_WebhookSubscription(Base):
    """Renamed from `WebhookSubscription` to avoid collision with the
    tenant-scoped `gdx_dispatch.routers.webhooks.WebhookSubscription`."""

    __tablename__ = "ss21_webhook_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(String(64), nullable=False, index=True)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False, index=True)
    url = Column(String(1024), nullable=False)
    events = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    disabled_at = Column(DateTime(timezone=True), nullable=True)

    signing_keys = relationship("WebhookSigningKey", back_populates="subscription")
    deliveries = relationship("SS21_WebhookDelivery", back_populates="subscription")


class WebhookSigningKey(Base):
    __tablename__ = "ss21_webhook_signing_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscription_id = Column(
        Integer,
        ForeignKey("ss21_webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    kid = Column(String(64), nullable=False, index=True)
    ciphertext = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    rotated_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    subscription = relationship("SS21_WebhookSubscription", back_populates="signing_keys")


class SS21_WebhookDelivery(Base):
    """Renamed from `WebhookDelivery` to avoid collision with the
    tenant-scoped `gdx_dispatch.routers.webhooks.WebhookDeliveryLog` model (and
    general clarity vs. the SS-21 specific attempt log)."""

    __tablename__ = "ss21_webhook_deliveries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscription_id = Column(
        Integer,
        ForeignKey("ss21_webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id = Column(String(64), nullable=False, index=True)
    attempt_number = Column(Integer, nullable=False, default=1)
    status_code = Column(Integer, nullable=True)
    error_type = Column(String(128), nullable=True)
    error_msg = Column(Text, nullable=True)
    attempted_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    succeeded = Column(Boolean, nullable=False, default=False)

    subscription = relationship("SS21_WebhookSubscription", back_populates="deliveries")


# ── SS-23: event-bus subscription + drain checkpoint ────────────────────────


class EventSubscription(Base):
    __tablename__ = "event_subscription"
    __table_args__ = (
        Index("ix_event_subscription_install", "installation_id"),
        Index("ix_event_subscription_event_name", "event_name"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    installation_id = Column(Uuid(as_uuid=True), nullable=False)
    event_name = Column(String(128), nullable=False)
    webhook_url = Column(String(1024), nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class EventDrainCheckpoint(Base):
    __tablename__ = "event_drain_checkpoint"
    __table_args__ = (
        Index("ix_event_drain_checkpoint_event", "event_outbox_id", unique=True),
        Index("ix_event_drain_checkpoint_status", "status"),
        Index("ix_event_drain_checkpoint_retry_after", "retry_after"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event_outbox_id = Column(Uuid(as_uuid=True), nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    retry_count = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    retry_after = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ── SS-24: metering + billing ───────────────────────────────────────────────


class MeteringUsage(Base):
    __tablename__ = "metering_usage"
    __table_args__ = (
        UniqueConstraint(
            "period_kind",
            "period_start",
            "tenant_id",
            "event_type",
            name="uq_metering_usage_period_tenant_event",
        ),
        Index("ix_metering_usage_tenant_period", "tenant_id", "period_start"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    period_kind = Column(String(16), nullable=False)
    period_start = Column(DateTime(timezone=True), nullable=False)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    event_type = Column(String(128), nullable=False)
    quantity = Column(BigInteger, nullable=False, default=0)
    aggregated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class MeteringCheckpoint(Base):
    __tablename__ = "metering_checkpoint"
    __table_args__ = (
        UniqueConstraint(
            "period_kind",
            "period_start",
            "tenant_id",
            "event_type",
            name="uq_metering_checkpoint_period_tenant_event",
        ),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    period_kind = Column(String(16), nullable=False)
    period_start = Column(DateTime(timezone=True), nullable=False)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    event_type = Column(String(128), nullable=False)
    last_event_id = Column(Uuid(as_uuid=True), nullable=True)
    last_emitted_at = Column(DateTime(timezone=True), nullable=True)
    quantity_total = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class BillingPlan(Base):
    __tablename__ = "billing_plan"
    __table_args__ = (
        Index("ix_billing_plan_tenant", "tenant_id", unique=True),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    plan_code = Column(String(64), nullable=False, default="free")
    period_kind = Column(String(16), nullable=False, default="month")
    limits = Column(JSON, nullable=False, default=dict)
    stripe_subscription_id = Column(String(128), nullable=True)
    stripe_subscription_item_id_by_event = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class BillingOverageEvent(Base):
    __tablename__ = "billing_overage_event"
    __table_args__ = (
        Index(
            "ix_billing_overage_period_tenant_event",
            "period_kind",
            "period_start",
            "tenant_id",
            "event_type",
            unique=True,
        ),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    event_type = Column(String(128), nullable=False)
    period_kind = Column(String(16), nullable=False)
    period_start = Column(DateTime(timezone=True), nullable=False)
    limit_value = Column(BigInteger, nullable=False)
    observed_quantity = Column(BigInteger, nullable=False)
    detected_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    emitted_event_id = Column(Uuid(as_uuid=True), nullable=True)
    notes = Column(String(512), nullable=True)


# ── SS-27: cross-tenant sharing ─────────────────────────────────────────────


class CrossTenantShare(Base):
    __tablename__ = "cross_tenant_share"
    __table_args__ = (
        Index("ix_cts_sharer", "sharer_tenant_id"),
        Index("ix_cts_sharee", "sharee_tenant_id"),
        Index("ix_cts_resource", "resource_type", "resource_id"),
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
    capabilities = Column(JSON, nullable=False)
    acceptance_token_hash = Column(String(128), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by_identity_id = Column(String(64), nullable=True)
    created_by_identity_id = Column(String(64), nullable=False)
    shared_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class CrossTenantShareAcceptance(Base):
    __tablename__ = "cross_tenant_share_acceptance"
    __table_args__ = (
        Index("ix_ctsa_share", "share_id"),
        UniqueConstraint("share_id", name="uq_ctsa_one_accept"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    share_id = Column(
        Uuid(as_uuid=True),
        ForeignKey("cross_tenant_share.id", ondelete="CASCADE"),
        nullable=False,
    )
    accepted_by_identity_id = Column(String(64), nullable=False)
    accepted_by_tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ── SS-28: platform consumer audit + retention policy ──────────────────────


class PlatformConsumerAudit(Base):
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
    result = Column(String(32), nullable=False)
    details = Column(JSON, nullable=True)
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    prev_hash = Column(String(64), nullable=False)
    row_hash = Column(String(64), nullable=False)


class AuditRetentionPolicy(Base):
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


# ── SS-29: shadow migration state + checkpoint + drift ──────────────────────


class ShadowMigrationState(Base):
    __tablename__ = "shadow_migration_state"
    __table_args__ = (
        UniqueConstraint("tenant_id", "old_table", name="uq_sms_tenant_table"),
        Index("ix_sms_tenant", "tenant_id"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    old_table = Column(String(128), nullable=False)
    new_table = Column(String(128), nullable=False)
    mode = Column(String(16), nullable=False, default="off")
    cutover_at = Column(DateTime(timezone=True), nullable=True)
    rolled_back_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ShadowMigrationCheckpoint(Base):
    __tablename__ = "shadow_migration_checkpoint"
    __table_args__ = (
        UniqueConstraint("tenant_id", "old_table", name="uq_smc_tenant_table"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    old_table = Column(String(128), nullable=False)
    last_row_id = Column(BigInteger, nullable=True)
    last_row_pk = Column(String(128), nullable=True)
    row_count_this_session = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ShadowMigrationDrift(Base):
    __tablename__ = "shadow_migration_drift"
    __table_args__ = (
        Index("ix_smd_tenant_created", "tenant_id", "created_at"),
        Index("ix_smd_table", "old_table"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    old_table = Column(String(128), nullable=False)
    reason = Column(String(64), nullable=False)
    old_hash = Column(String(64), nullable=True)
    new_hash = Column(String(64), nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ── SS-30: cutover schedule + deprecated table record ───────────────────────


class CutoverSchedule(Base):
    __tablename__ = "cutover_schedule"
    __table_args__ = (
        UniqueConstraint("tenant_id", "old_table", name="uq_cs_tenant_table"),
        Index("ix_cs_scheduled_drop_at", "scheduled_drop_at"),
        Index("ix_cs_tenant", "tenant_id"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    old_table = Column(String(128), nullable=False)
    new_table = Column(String(128), nullable=False)
    deprecated_table = Column(String(160), nullable=False)
    executed_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    scheduled_drop_at = Column(DateTime(timezone=True), nullable=False)
    extended_count = Column(String(8), nullable=False, default="0")
    dry_run = Column(Boolean, nullable=False, default=False)
    dropped_at = Column(DateTime(timezone=True), nullable=True)
    actor_identity_id = Column(String(64), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class DeprecatedTableRecord(Base):
    __tablename__ = "deprecated_table_record"
    __table_args__ = (
        Index("ix_dtr_tenant", "tenant_id"),
        Index("ix_dtr_dropped_at", "dropped_at"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    old_table = Column(String(128), nullable=True)
    deprecated_table = Column(String(160), nullable=False)
    scheduled_drop_at = Column(DateTime(timezone=True), nullable=False)
    dropped_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    dry_run = Column(Boolean, nullable=False, default=False)
    actor_identity_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ── SS-31: federation provider + link + trust bundle cache ──────────────────


class FederationProvider(Base):
    __tablename__ = "federation_provider"
    __table_args__ = (
        Index("ix_fed_provider_tenant", "tenant_id"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    kind = Column(String(16), nullable=False)
    display_name = Column(String(255), nullable=False)
    metadata_url = Column(String(1024), nullable=False)
    client_id = Column(String(255), nullable=True)
    client_secret_encrypted = Column(Text, nullable=True)
    trust_bundle_ref = Column(String(255), nullable=True)
    redirect_uri = Column(String(1024), nullable=True)
    sp_entity_id = Column(String(255), nullable=True)
    acs_url = Column(String(1024), nullable=True)
    scope = Column(String(255), nullable=True, default="openid email profile")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class FederationLink(Base):
    __tablename__ = "federation_link"
    __table_args__ = (
        UniqueConstraint("provider_id", "external_subject", name="uq_fed_link_provider_subject"),
        Index("ix_fed_link_identity", "identity_id"),
        Index("ix_fed_link_provider", "provider_id"),
        Index("ix_fed_link_external_subject", "external_subject"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    identity_id = Column(
        Uuid(as_uuid=True), ForeignKey("identities.id", ondelete="CASCADE"), nullable=False
    )
    provider_id = Column(
        Uuid(as_uuid=True), ForeignKey("federation_provider.id", ondelete="CASCADE"), nullable=False
    )
    external_subject = Column(String(255), nullable=False)
    linked_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class FederationTrustBundleCache(Base):
    __tablename__ = "federation_trust_bundle_cache"

    provider_id = Column(
        Uuid(as_uuid=True),
        ForeignKey("federation_provider.id", ondelete="CASCADE"),
        primary_key=True,
    )
    bundle_json = Column(Text, nullable=False)
    fetched_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    ttl_seconds = Column(Integer, nullable=False, default=3600)
    last_refresh_error = Column(Text, nullable=True)


# ── SS-32: SPIFFE workload registration + trust bundle cache ────────────────


class SpiffeWorkloadRegistration(Base):
    __tablename__ = "spiffe_workload_registration"
    __table_args__ = (
        Index("ix_spiffe_workload_glob", "spiffe_id_glob", unique=True),
        Index("ix_spiffe_workload_scope", "tenant_scope"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    spiffe_id_glob = Column(String(512), nullable=False)
    capabilities = Column(JSON, nullable=False, default=list)
    tenant_scope = Column(String(32), nullable=False, default="per-tenant")
    spiffe_metadata = Column(JSON, nullable=True)
    registered_by_identity_id = Column(String(64), nullable=False)
    notes = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class SpiffeTrustBundleCache(Base):
    __tablename__ = "spiffe_trust_bundle_cache"
    __table_args__ = (
        Index("ix_spiffe_bundle_td", "trust_domain", unique=True),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    trust_domain = Column(String(255), nullable=False)
    bundle_json = Column(JSON, nullable=False)
    fetched_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    ttl_seconds = Column(String(16), nullable=False, default="300")
    source_endpoint = Column(String(512), nullable=True)
    last_refresh_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


# ── SS-33: resource extensibility (type + instance + deletion request) ──────


class ResourceType(Base):
    __tablename__ = "resource_type"
    __table_args__ = (
        UniqueConstraint("name", name="uq_resource_type_name"),
        Index("ix_resource_type_owner", "owner_tenant_id"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(160), nullable=False)
    json_schema = Column(JSON, nullable=False)
    capabilities = Column(JSON, nullable=False, default=list)
    index_hints = Column(JSON, nullable=False, default=list)
    owner_tenant_id = Column(Uuid(as_uuid=True), nullable=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ResourceInstance(Base):
    __tablename__ = "resource_instance"
    __table_args__ = (
        Index("ix_resource_instance_tenant_type", "tenant_id", "type_name"),
        Index("ix_resource_instance_type", "type_name"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id = Column(Uuid(as_uuid=True), nullable=False)
    type_name = Column(String(160), nullable=False)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class ResourceTypeDeletionRequest(Base):
    __tablename__ = "resource_type_deletion_request"
    __table_args__ = (
        UniqueConstraint("name", name="uq_rtdr_name"),
        Index("ix_rtdr_scheduled_for", "scheduled_for"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(160), nullable=False)
    requested_by_identity_id = Column(String(64), nullable=False)
    reason = Column(Text, nullable=True)
    requested_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    scheduled_for = Column(DateTime(timezone=True), nullable=False)
    executed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)


# ── SS-34: DR snapshot manifest + drill run + verification report ───────────


class DrSnapshotManifest(Base):
    __tablename__ = "dr_snapshot_manifest"
    __table_args__ = (
        Index("ix_dr_snap_created", "created_at"),
        Index("ix_dr_snap_sha", "sha256"),
    )

    id = Column(String(128), primary_key=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    size_bytes = Column(BigInteger, nullable=False)
    sha256 = Column(String(64), nullable=False)
    scope_description = Column(String(256), nullable=False)
    backup_location = Column(Text, nullable=False)
    source_db_redacted = Column(Text, nullable=True)


class DrDrillRun(Base):
    __tablename__ = "dr_drill_run"
    __table_args__ = (
        Index("ix_dr_drill_scheduled", "scheduled_for"),
        Index("ix_dr_drill_status", "passed"),
    )

    drill_run_id = Column(String(64), primary_key=True)
    scheduled_for = Column(DateTime(timezone=True), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    scope = Column(String(32), nullable=False)
    scope_selector = Column(String(256), nullable=True)
    dry_run = Column(Boolean, nullable=False, default=False)
    passed = Column(Boolean, nullable=False, default=False)
    failure_reason = Column(Text, nullable=True)
    snapshot_id = Column(String(128), nullable=True)
    scheduled_by_identity_id = Column(String(64), nullable=True)
    staging_db_redacted = Column(Text, nullable=True)
    report_json = Column(JSON, nullable=True)


class DrVerificationReport(Base):
    __tablename__ = "dr_verification_report"
    __table_args__ = (
        Index("ix_dr_verify_drill", "drill_run_id"),
        Index("ix_dr_verify_created", "created_at"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    drill_run_id = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    passed = Column(Boolean, nullable=False, default=False)
    failed_count = Column(BigInteger, nullable=False, default=0)
    checks_json = Column(JSON, nullable=False)


# ── SS-35: SAR request + erasure request + PII event log ────────────────────


class SARRequest(Base):
    __tablename__ = "sar_request"
    __table_args__ = (
        Index("ix_sar_target", "target_identity_id"),
        Index("ix_sar_status", "status"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    target_identity_id = Column(String(64), nullable=False)
    requested_by_identity_id = Column(String(64), nullable=False)
    reason = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="queued")
    export_json = Column(JSON, nullable=True)
    download_token = Column(String(256), nullable=True)
    download_issued_at = Column(String(64), nullable=True)
    downloaded_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ErasureRequest(Base):
    __tablename__ = "erasure_request"
    __table_args__ = (
        Index("ix_erasure_target", "target_identity_id"),
        Index("ix_erasure_status", "status"),
        Index("ix_erasure_cooloff", "cooloff_until"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    target_identity_id = Column(String(64), nullable=False)
    requested_by_identity_id = Column(String(64), nullable=False)
    reason = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="queued")
    cooloff_until = Column(DateTime(timezone=True), nullable=False)
    approved_by_identity_id = Column(String(64), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    executed_at = Column(DateTime(timezone=True), nullable=True)
    affected_field_count = Column(BigInteger, nullable=True)
    affected_summary = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class PIIEventLog(Base):
    __tablename__ = "pii_event_log"
    __table_args__ = (
        Index("ix_pii_log_identity", "target_identity_id"),
        Index("ix_pii_log_event", "event_name"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event_name = Column(String(128), nullable=False)
    target_identity_id = Column(String(64), nullable=False)
    payload = Column(JSON, nullable=True)
    prev_hash = Column(String(64), nullable=False, default="")
    entry_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
