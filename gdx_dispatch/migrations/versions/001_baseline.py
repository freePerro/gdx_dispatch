"""Squashed baseline — all control plane tables from ORM as of 2026-04-12.

Revision ID: 001_baseline
Revises:
Create Date: 2026-04-12

Replaces: 001_initial_control_plane, 050_multi_location, 060_game_system.
The previous 3 migrations were squashed into this single baseline after
the D1 nuke-and-pave verified the ORM is the source of truth.

This migration is ALREADY APPLIED to the production control DB —
stamp with: alembic stamp 001_baseline
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSON, UUID

revision = "001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # tenants
    op.create_table(
        "tenants",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("db_url_enc", sa.String(), nullable=False),
        sa.Column("db_provisioned", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("stripe_customer_id", sa.String(100), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(100), nullable=True),
        sa.Column("subscription_status", sa.String(20), nullable=False, server_default="trialing"),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(60), nullable=False, server_default="America/New_York"),
        sa.Column("welcome_email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stripe_connect_account_id", sa.String(100), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )

    # tenant_module_grants
    op.create_table(
        "tenant_module_grants",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("module_key", sa.String(100), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("granted_by", sa.String(200), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # stripe_webhook_events
    op.create_table(
        "stripe_webhook_events",
        sa.Column("id", sa.String(100), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )

    # platform_feature_flags
    op.create_table(
        "platform_feature_flags",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("flag_key", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("flag_key", "tenant_id", name="uq_feature_flag_key_tenant"),
    )

    # game_definitions
    op.create_table(
        "game_definitions",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon", sa.String(50), nullable=True),
        sa.Column("actor_type", sa.String(50), nullable=False, server_default="claude"),
        sa.Column("publisher", sa.String(100), nullable=False, server_default="system"),
        sa.Column("layout_json", JSON(), nullable=False, server_default="{}"),
        sa.Column("rules_json", JSON(), nullable=False, server_default="{}"),
        sa.Column("tenant_id", sa.String(100), nullable=True),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )

    # game_state
    op.create_table(
        "game_state",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("game_slug", sa.String(100), sa.ForeignKey("game_definitions.slug", ondelete="RESTRICT"), nullable=False),
        sa.Column("tenant_id", sa.String(100), nullable=True),
        sa.Column("lives", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("max_lives", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("hp", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("max_hp", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("xp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_phase", sa.String(100), nullable=True),
        sa.Column("state_json", JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_id", "game_slug", name="uq_game_state_actor_game"),
    )

    # game_events
    op.create_table(
        "game_events",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("game_slug", sa.String(100), sa.ForeignKey("game_definitions.slug", ondelete="RESTRICT"), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("value", sa.Integer(), nullable=True),
        sa.Column("value_string", sa.String(500), nullable=True),
        sa.Column("reason", sa.String(2000), nullable=True),
        sa.Column("created_by_user_id", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("game_events")
    op.drop_table("game_state")
    op.drop_table("game_definitions")
    op.drop_table("platform_feature_flags")
    op.drop_table("stripe_webhook_events")
    op.drop_table("tenant_module_grants")
    op.drop_table("tenants")
