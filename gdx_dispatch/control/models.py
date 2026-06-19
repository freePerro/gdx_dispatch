from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import Uuid


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    stripe_connect_account_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Kept (SCOPE.md): not SaaS-billing here — task SQL still filters on the physical
    # column and rate_limiter reads it off request.state.tenant. Maps the existing
    # NOT NULL DEFAULT 'trialing' column; its CheckConstraint stays dropped.
    subscription_status: Mapped[str] = mapped_column(String(20), nullable=False, default="trialing")
    timezone: Mapped[str] = mapped_column(String(60), nullable=False, default="America/New_York")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    street: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(80))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country: Mapped[str | None] = mapped_column(String(2))
    phone: Mapped[str | None] = mapped_column(String(32))
    employee_count: Mapped[int | None] = mapped_column(Integer)
    industry: Mapped[str | None] = mapped_column(String(64))


class TenantModuleGrant(Base):
    __tablename__ = "tenant_module_grants"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    module_key: Mapped[str] = mapped_column(String(50), nullable=False)
    granted_by_tenant_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id"))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformFeatureFlag(Base):
    """Platform-wide feature flags with rollout percentages."""
    __tablename__ = "platform_feature_flags"
    __table_args__ = (
        CheckConstraint("rollout_pct >= 0 AND rollout_pct <= 100", name="ck_platform_ff_rollout_pct"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    flag_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    default_value: Mapped[bool] = mapped_column(default=False, nullable=False)
    rollout_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tenant_overrides: Mapped[dict[str, bool]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


FeatureFlag = PlatformFeatureFlag


class GameDefinition(Base):
    __tablename__ = "game_definitions"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_game_definitions_slug"),
        Index("ix_game_definitions_tenant_id", "tenant_id"),
        Index("ix_game_definitions_actor_type", "actor_type"),
        Index("ix_game_definitions_is_published", "is_published"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(String(100))
    actor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    publisher: Mapped[str] = mapped_column(String(100), nullable=False, default="House")
    layout_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    rules_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    tenant_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"))
    created_by: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GameState(Base):
    __tablename__ = "game_state"
    __table_args__ = (
        UniqueConstraint("actor_id", "game_slug", name="uq_game_state_actor_game"),
        Index("ix_game_state_actor_id", "actor_id"),
        Index("ix_game_state_game_slug", "game_slug"),
        Index("ix_game_state_tenant_id", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    game_slug: Mapped[str] = mapped_column(String(100), ForeignKey("game_definitions.slug", ondelete="RESTRICT"), nullable=False)
    tenant_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"))
    lives: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    max_lives: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    hp: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    max_hp: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    xp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_phase: Mapped[str | None] = mapped_column(String(100))
    state_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class GameEvent(Base):
    __tablename__ = "game_events"
    __table_args__ = (
        Index("ix_game_events_actor_id", "actor_id"),
        Index("ix_game_events_game_slug", "game_slug"),
        Index("ix_game_events_created_at", "created_at"),
        Index("ix_game_events_actor_game_time", "actor_id", "game_slug", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    game_slug: Mapped[str] = mapped_column(String(100), ForeignKey("game_definitions.slug", ondelete="RESTRICT"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    value: Mapped[int | None] = mapped_column(Integer)
    value_string: Mapped[str | None] = mapped_column(String(500))
    reason: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ServiceAccount(Base):
    __tablename__ = "service_accounts"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_service_accounts_key_hash"),
        UniqueConstraint("name", name="uq_service_accounts_name"),
        Index("ix_service_accounts_key_prefix", "key_prefix"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    allowed_tenant_uuids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    allowed_scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TenantSettings(Base):
    __tablename__ = "tenant_settings"

    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    llm_provider_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_provider_key_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    llm_provider_key_last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    llm_provider_key_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_com_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_com_token_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    phone_com_token_last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    phone_com_token_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_com_webhook_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_com_webhook_callback_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    phone_com_webhook_listener_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    phone_com_webhook_secret_prev: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_com_webhook_secret_prev_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    phone_com_webhook_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outlook_microsoft_tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outlook_client_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outlook_client_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    outlook_secret_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    estimate_draft_archive_days: Mapped[int] = mapped_column(Integer, nullable=False, default=60, server_default="60")
    estimate_deposit_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default="50")
    job_number_format: Mapped[str] = mapped_column(String(200), nullable=False, default="JOB-{year}-{seq:03d}", server_default="JOB-{year}-{seq:03d}")
    job_number_next_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    job_number_year_seen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    workflow_lock_schedule_on_start: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    workflow_post_arrival_event: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    workflow_sms_arrival_notify: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    workflow_require_parts_on_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    workflow_require_hours_on_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    workflow_require_signature_on_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    dispatch_warn_save_no_tech: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    dispatch_block_save_no_tech: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    dispatch_show_unassigned_lane: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    default_payment_terms_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")
    contractor_payment_terms_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retail_payment_terms_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wholesale_payment_terms_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    early_pay_discount_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    early_pay_discount_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    late_fee_flat_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    late_fee_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    late_fee_grace_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    interest_rate_monthly_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    interest_grace_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    catalog_require_description: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    catalog_render_name_when_desc_empty: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    catalog_ai_suggest_descriptions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    catalog_block_zero_price_on_invoice: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    catalog_warn_zero_price_on_invoice: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    catalog_block_zero_price_on_save: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    catalog_auto_inactivate_zero_price: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    payroll_source: Mapped[str] = mapped_column(String(40), nullable=False, default="manual", server_default="manual")
    maps_provider: Mapped[str] = mapped_column(String(40), nullable=False, default="google_maps", server_default="google_maps")
    estimates_allow_line_margin_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
